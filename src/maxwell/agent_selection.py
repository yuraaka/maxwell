import random
from collections import defaultdict

from maxwell.config_apply import config_digest
from maxwell.reputation import selection_evidence
from maxwell.stages import STAGE_ROLES, canonical_stage
from maxwell.store import Store


class AgentSelector:
    def __init__(
        self,
        paths,
        config,
        *,
        provider=None,
        model=None,
        effort=None,
        rng=None,
        evidence_provider=None,
    ):
        self.config = config
        self.rng = rng or random.SystemRandom()
        self.evidence_provider = evidence_provider or selection_evidence
        self.explicit = model is not None
        self.routes = defaultdict(list)
        if self.explicit:
            route = config.route(provider, model, effort=effort)
            self.add(route)
            return
        if provider is not None:
            config.provider_name(provider)
        metadata = Store.cached_models(paths.db, provider)
        if metadata["config_digest"] != config_digest(paths):
            raise ValueError("Model metadata is stale; run `maxwell config apply`")
        for catalog in metadata["providers"]:
            name = catalog["provider"]
            if name not in config.all_providers():
                continue
            for entry in catalog["models"]:
                if entry.get("access") != "verified" or not entry.get("selectable", True):
                    continue
                if (
                    effort not in (None, "default")
                    and entry.get("efforts") is not None
                    and effort not in entry["efforts"]
                ):
                    continue
                try:
                    route = config.route(name, entry["model"], effort=effort)
                    self.add(route)
                except ValueError:
                    continue
        if not self.routes:
            raise ValueError(
                "No verified selectable models match; run `maxwell config apply` or pin --model and --provider"
            )

    def add(self, route):
        settings = self.config.execution(route, {})["settings"]
        identity = (settings["adapter"], route["model"], settings["effort"])
        self.routes[identity].append(route)

    def select(self, store, repository, stage, *, avoid=()):
        stage = canonical_stage(stage)
        candidates = sorted(self.routes, key=lambda item: tuple(value or "" for value in item))
        avoided = set(avoid)
        distinct = [candidate for candidate in candidates if candidate[:2] not in avoided]
        independence = (
            "not_applicable" if not avoid else "different_model" if distinct else "constrained"
        )
        if self.explicit:
            independence = (
                "not_applicable" if not avoid else "different_model" if distinct else "overridden"
            )
        elif distinct:
            candidates = distinct
        evidence = self.evidence_provider(store, repository, STAGE_ROLES[stage])
        unrated_stage = stage in ("audit", "fix", "review", "integrate") and not any(
            sum(evidence.get(candidate, [0, 0])) for candidate in candidates
        )
        if self.explicit or unrated_stage:
            identity = self.rng.choice(candidates)
            draw = None
            method = "explicit" if self.explicit else "uniform-unrated-v1"
        else:
            draws = {
                candidate: self.rng.betavariate(
                    2 + evidence.get(candidate, [0, 0])[0], 2 + evidence.get(candidate, [0, 0])[1]
                )
                for candidate in candidates
            }
            identity = max(draws, key=draws.get)
            draw = draws[identity]
            method = "thompson-repair-v1" if stage == "fix" else "thompson-source-v1"
        successes, failures = evidence.get(identity, [0, 0])
        return self.rng.choice(self.routes[identity]), {
            "method": method,
            "successes": successes,
            "failures": failures,
            "draw": draw,
            "candidates": len(candidates),
            "independence": independence,
        }
