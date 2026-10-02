import httpx


class PublicationUncertain(Exception):
    pass


class GitHub:
    def __init__(self, settings):
        if not settings.token.get_secret_value():
            raise ValueError("Configure a token for the repository's GitHub account")
        self.allowed_users = settings.allowed_users
        self.client = httpx.Client(
            base_url=settings.api_url.rstrip("/") + "/",
            timeout=15,
            headers={
                "Authorization": f"Bearer {settings.token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def close(self):
        self.client.close()

    def request(self, method, path, **kwargs):
        response = self.client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def paginate(self, path, params=None):
        for page in range(1, 101):
            batch = self.request(
                "GET", path, params={**(params or {}), "per_page": 100, "page": page}
            )
            yield from batch
            if len(batch) < 100:
                return
        raise ValueError("GitHub pagination limit reached; reconciliation incomplete")

    def find_issue(self, repo, marker):
        for issue in self.paginate(f"repos/{repo}/issues", {"state": "all"}):
            if "pull_request" not in issue and marker in (issue.get("body") or ""):
                return issue
        return None

    def create_issue(self, repo, title, body, labels=()):
        try:
            return self.request(
                "POST",
                f"repos/{repo}/issues",
                json={"title": title, "body": body, "labels": list(labels)},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                raise PublicationUncertain(
                    "GitHub may have created the issue; reconciliation required"
                ) from exc
            raise
        except httpx.TransportError as exc:
            raise PublicationUncertain(
                "Issue creation response lost; reconciliation required"
            ) from exc

    def ensure_labels(self, repo):
        labels = {
            "maxwell": "8250df",
            "maxwell:state:suspected": "fbca04",
            "maxwell:state:human-confirmed": "0e8a16",
            "maxwell:state:rejected": "eeeeee",
            **{f"maxwell:priority:P{i}": "d4c5f9" for i in range(4)},
        }
        for name, color in labels.items():
            response = self.client.post(f"repos/{repo}/labels", json={"name": name, "color": color})
            if response.status_code != 422:
                response.raise_for_status()

    def comments(self, repo, issue):
        return list(self.paginate(f"repos/{repo}/issues/{issue}/comments"))

    def upsert_comment(self, repo, issue, marker, body):
        for comment in self.comments(repo, issue):
            if marker in comment["body"]:
                if comment["body"] != body:
                    self.request(
                        "PATCH",
                        f"repos/{repo}/issues/comments/{comment['id']}",
                        json={"body": body},
                    )
                return
        self.request("POST", f"repos/{repo}/issues/{issue}/comments", json={"body": body})
