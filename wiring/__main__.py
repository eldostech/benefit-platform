"""``python -m wiring discover | apply [--dry-run] | check``, with the
Workbench's interpreter (see wiring/__init__.py)."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from wiring.documents import CHANGE_TYPES, PLATFORMS, admin_system, channel_rule, manifest, trace_manifest
from wiring.workbench import workbench

REASON = "Wire the real Platform A and Platform B (benefit-platforms) in place of the sandbox"


async def _audit(service: Any, actor: Any, action: str, resource_type: str, resource_id: str, detail: dict[str, Any]) -> None:
    await service.store.workspace(actor.workspace_id).audit(
        action, actor_id=actor.user_id, actor_email=actor.email, resource_type=resource_type, resource_id=resource_id, detail=detail
    )


async def _drafts(service: Any, actor: Any) -> list[tuple[str, dict[str, Any]]]:
    """Each platform's REST server, and its TRACE server where it has one,
    as the Workbench drafts them from the live servers."""
    drafts = []
    for code, p in PLATFORMS.items():
        found = await service.connectors.discover(
            actor.workspace_id, code, {"kind": "http", "url": p["mcp_url"]}, purpose="rest", name=p["name"]
        )
        if not found["registrable"]:
            raise SystemExit(f"{code}: the Workbench will not register {p['mcp_url']}: {json.dumps(found['problems'])}")
        drafts.append((code, manifest(code, found["draft"])))
        if p.get("trace"):
            url = p["trace"]["mcp_url"]
            try:
                found = await service.connectors.discover(actor.workspace_id, code, {"kind": "http", "url": url}, purpose="trace")
            except Exception as exc:  # noqa: BLE001 - said plainly: the rules name a server that is not there
                raise SystemExit(f"{code}: TRACE's MCP server did not answer at {url} ({exc}); start it and run this again.") from None
            # TRACE lists every use case; only the tools taken need pass.
            problems = [x for x in found["problems"] if x["tool"] in p["trace"]["tools"]]
            if problems:
                raise SystemExit(f"{code}: the Workbench will not register TRACE's tools: {json.dumps(problems)}")
            drafts.append((code, trace_manifest(code, found["draft"])))
    return drafts


async def discover() -> None:
    async with workbench() as (service, actor):
        for code, doc in await _drafts(service, actor):
            print(f"{code}: {doc['id']} registrable, tools: {', '.join(c['name'] for c in doc['capabilities'])}")


async def apply(dry_run: bool) -> None:
    async with workbench() as (service, actor):
        ws = actor.workspace_id
        routing = service.routing.workspace(ws)
        who = f"user:{actor.user_id}"

        # 1. The MCP servers, each checked against its live tool list.
        for code, doc in await _drafts(service, actor):
            if dry_run:
                seen = await service.connectors.preview(doc, {}, workspace_id=ws)
                print(f"[dry run] connector {doc['id']}: matches its live tools: {seen['matches_manifest']}")
                continue
            public = await service.connectors.register(ws, doc, {}, actor, enabled=True, verify=True)
            service.ensure_platform_agent(code)
            await _audit(
                service,
                actor,
                "connector.save",
                "connector",
                public["id"],
                {
                    "version": public["version"],
                    "manifest_hash": public["manifest_hash"],
                    "pattern": public.get("pattern"),
                    "admin_system": code,
                    "enabled": public["enabled"],
                    "transport": "http",
                    "verified": True,
                },
            )
            print(f"connector {public['id']}: registered, {len(doc['capabilities'])} tools, platform agent platform.{code}")

        if dry_run:
            print("[dry run] nothing written. Without --dry-run: the servers above, 2 admin systems, "
                  f"{len(CHANGE_TYPES)} change types and {len(CHANGE_TYPES) * len(PLATFORMS)} channel rules; "
                  "stored employers and plans, if any, are removed.")
            return

        # 2-4. Routing: admin systems, change types, one rule per (system, change type).
        current = {s["code"]: s for s in await routing.list_admin_systems()}

        async def write(action: str, kind: str, rid: str, result: dict[str, Any]) -> None:
            validation = result.get("validation") or {}
            if validation and not validation.get("ok", True):
                raise SystemExit(f"{kind} {rid} refused: {json.dumps(validation)}")
            state = "changed" if result.get("changed") else "unchanged"
            if result.get("changed"):
                await _audit(
                    service,
                    actor,
                    action,
                    kind,
                    rid,
                    {
                        "before_version": result.get("before_version"),
                        "after_version": result.get("version"),
                        "reason": REASON,
                        "validation": validation,
                        "data_version": result.get("data_version"),
                    },
                )
            print(f"{kind} {rid}: {state}")

        for code in PLATFORMS:
            value = admin_system(code, current.get(code, {}))
            await write(
                "routing.admin_system.upsert",
                "routing_admin_system",
                code,
                await routing.upsert_admin_system(value, expect_version=current.get(code, {}).get("version"), reason=REASON, actor=who),
            )
        existing = {c["id"]: c for c in await routing.list_change_types_full()}
        for ct in CHANGE_TYPES:
            await write(
                "routing.change_type.upsert",
                "routing_change_type",
                ct["id"],
                await routing.upsert_change_type(ct, expect_version=existing.get(ct["id"], {}).get("version"), reason=REASON, actor=who),
            )
        rules = {(r["admin_system"], r["change_type_id"]): r for r in await routing.list_channel_rules()}
        for ct in CHANGE_TYPES:
            for code in PLATFORMS:
                await write(
                    "routing.channel_rule.upsert",
                    "routing_channel_rule",
                    f"{code}/{ct['id']}",
                    await routing.upsert_channel_rule(
                        code,
                        ct["id"],
                        channel_rule(code, ct),
                        expect_version=rules.get((code, ct["id"]), {}).get("version"),
                        reason=REASON,
                        actor=who,
                    ),
                )

        # 5. No employers or plans in the Workbench: the platforms are the
        #    systems of record, and routing asks the plan directory for them
        #    live (ROUTING_PLAN_DIRECTORY_URL). Any stored copy goes.
        removed = await _forget_plans(service, actor)
        print(f"stored employers and plans removed: {removed}")


async def _forget_plans(service: Any, actor: Any) -> dict[str, int]:
    """Delete every stored employer, plan and key of the workspace, in one
    transaction, with an audit entry. Decisions keep their own record of what
    they saw (`routing_decisions`, `routing_resolved_targets`): that is the
    history of what was decided, not a copy of the systems of record."""
    from sqlalchemy import delete

    from infra.db.workbench_models import RoutingEmployer, RoutingEmployerSystemKey, RoutingPlan, RoutingPlanKey

    ws = actor.workspace_id
    counts: dict[str, int] = {}
    async with service.store.sessions() as session:
        for model in (RoutingPlanKey, RoutingPlan, RoutingEmployerSystemKey, RoutingEmployer):
            result = await session.execute(delete(model).where(model.workspace_id == ws))
            counts[model.__tablename__] = int(result.rowcount or 0)
        await session.commit()
    if any(counts.values()):
        await _audit(service, actor, "routing.plans_removed", "routing", ws, {"reason": "plans are read live from the plan directory", **counts})
    return counts


async def check() -> None:
    """What the Workbench would do, without a request: each change type
    resolved for a customer, and one read through each platform's server by
    the same path an agent's call takes."""
    cases = [
        ("address.change", "00104217", None),
        ("coverage.add.disability", "00104217", ["disability.std"]),
        ("coverage.add.dental", "01555123", None),
        ("coverage.add.vision", "00030561", None),
        ("coverage.terminate", "00104217", ["disability.std"]),
        ("contribution.change", "00255190", None),
        ("billing.change", "01022045", None),
        ("address.change", "01300072", None),
    ]
    async with workbench() as (service, actor):
        routing = service.routing.workspace(actor.workspace_id)
        overview = await routing.overview()
        print("plans come from:", overview.get("plan_directory") or "THE STORED INDEX (ROUTING_PLAN_DIRECTORY_URL is not set)")
        print("stored:", {k: v for k, v in overview["counts"].items() if k in ("employers", "plans")} or "no employer or plan counts (none stored)")
        for change_type, employer, lines in cases:
            d = await routing.resolve_change(change_type_id=change_type, employer_ref=employer, product_line_ids=lines)
            targets = [f"{t['plan_id']}@{t['admin_system']['code']} via {t.get('connector_id')}" for t in d["targets"]]
            refused = [f"{a.get('kind')}" for a in d.get("ambiguities") or []]
            print(f"{change_type:24} {employer}: {targets or 'no targets'}{'  ambiguities ' + str(refused) if refused else ''}")
        by_name = await routing.locate_plans(name="Harbor Point")
        print("by name 'Harbor Point':", by_name["employer"] and by_name["employer"]["employer_ref"],
              [(x["plan_id"], x["system_of_record"]["code"]) for x in by_name["plans"]])


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m wiring")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("discover")
    a = sub.add_parser("apply")
    a.add_argument("--dry-run", action="store_true")
    sub.add_parser("check")
    args = parser.parse_args()
    if args.command == "discover":
        asyncio.run(discover())
    elif args.command == "apply":
        asyncio.run(apply(args.dry_run))
    else:
        asyncio.run(check())


if __name__ == "__main__":
    main()
