"""Command manifests vs what RobotRay2 offered (RobotRay ADR-029 P4).

docs/command-inventory.json is the snapshot of the old web menu, the
Telegram keyboards and each service's own dispatch tokens
(written by fin's scripts/command_inventory.py). A manifest may add commands; it may not
drop one.
"""

import importlib
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INVENTORY = json.loads((REPO / "docs/command-inventory.json").read_text())

SERVICES = {
    "jobhunter": "hunter.services.jobhunter_service.JobHunterService",
}

NOT_COMMANDS = {}


def _manifest(service):
    module, cls = SERVICES[service].rsplit(".", 1)
    return getattr(importlib.import_module(module), cls).describe_commands()


def _old_tokens(service):
    web = {c["token"] for c in INVENTORY["web"][service]["commands"]}
    telegram = {t.split()[0] for t in INVENTORY["telegram"][service]}
    return (web | telegram | set(INVENTORY["dispatch"][service])) - NOT_COMMANDS.get(service, set())


@pytest.mark.parametrize("service", sorted(SERVICES))
class TestParity:
    def test_service_declares_its_manifest(self, service):
        manifest = _manifest(service)
        assert manifest is not None and manifest.service == service
        assert manifest.summary and manifest.telegram_command == service

    def test_no_command_was_dropped(self, service):
        manifest = _manifest(service)
        names = {c.name for c in manifest.commands} | {
            a for c in manifest.commands for a in c.aliases}
        assert _old_tokens(service) <= names, sorted(_old_tokens(service) - names)

    def test_every_web_menu_command_is_still_in_the_menu(self, service):
        manifest = _manifest(service)
        for row in INVENTORY["web"][service]["commands"]:
            if row["token"] == "help":
                continue
            command = manifest.command(row["token"])
            assert command is not None and command.menu, row["token"]

    def test_every_web_form_field_survives(self, service):
        manifest = _manifest(service)
        for row in INVENTORY["web"][service]["commands"]:
            have = {p.name for p in manifest.command(row["token"]).params}
            want = {p["name"] for p in row["params"]}
            assert want <= have, (row["token"], sorted(want - have))

    def test_a_command_with_a_bare_menu_entry_needs_no_input(self, service):
        """`@keydates upcoming` was a one-tap button; its form field must
        stay optional or the tap turns into a form."""
        manifest = _manifest(service)
        for row in INVENTORY["web"][service]["commands"]:
            if row["params"] or row["args"] or row["requires_input"]:
                continue
            assert not manifest.command(row["token"]).requires_input, row["token"]

    def test_text_is_finished(self, service):
        manifest = _manifest(service)
        for command in manifest.commands:
            assert command.summary and "TODO" not in command.summary, command.name
            assert command.summary[0].isupper() or not command.summary[0].isalpha(), command.name

    def test_destructive_commands_ask_first(self, service):
        manifest = _manifest(service)
        for command in manifest.commands:
            if command.menu and command.name in ("delete", "remove", "unlink-external"):
                assert command.confirm, command.name
