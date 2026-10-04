import argparse

from maxwell.cli_output import color


class TreeParser(argparse.ArgumentParser):
    def format_help(self):
        subcommands = next(
            (action for action in self._actions if isinstance(action, argparse._SubParsersAction)),
            None,
        )
        if subcommands is None:
            return super().format_help()
        formatter = self._get_formatter()
        formatter.add_text(self.description)
        actions = [
            action for action in self._actions if not isinstance(action, argparse._SubParsersAction)
        ]
        usage = formatter._format_actions_usage(actions, self._mutually_exclusive_groups)
        formatter.add_usage(
            f"%(prog)s {usage} <command>", self._actions, self._mutually_exclusive_groups
        )
        formatter.start_section("options")
        formatter.add_arguments(
            [
                action
                for action in self._actions
                if not isinstance(action, argparse._SubParsersAction)
            ]
        )
        formatter.end_section()
        lines = [formatter.format_help().rstrip(), "", color("Commands", "1;36")]

        def tree(action, prefix=""):
            descriptions = {choice.dest: choice.help for choice in action._choices_actions}
            children = list(action.choices.items())
            for index, (name, command) in enumerate(children):
                last = index == len(children) - 1
                branch = "└── " if last else "├── "
                description = descriptions.get(name, "")
                lines.append(
                    prefix
                    + branch
                    + color(name, "1;36")
                    + (f"  {description}" if description else "")
                )
                nested = next(
                    (
                        item
                        for item in command._actions
                        if isinstance(item, argparse._SubParsersAction)
                    ),
                    None,
                )
                continuation = prefix + ("    " if last else "│   ")
                if nested:
                    tree(nested, continuation)

        tree(subcommands)
        lines.extend(["", f"Run `{self.prog} <command> --help` for options.", ""])
        return "\n".join(lines)
