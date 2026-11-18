from __future__ import annotations

import uuid

from deskweave.adapters.run_commands import RunCommand
from deskweave.adapters.workspace import utc_time
from deskweave.errors import DeskWeaveError
from deskweave.json import digest


class RunAPI:
    def __init__(self, engine):
        self.engine, self.workspace = engine, engine.workspace
        self.plans = {}

    def release(self, owner):
        self.plans = {k: v for k, v in self.plans.items() if v["owner"] != owner}

    def prepare(self, session, params, _request_id):
        access = self.engine.run_access(session)
        plan, report = self.engine.runs.planner.inspect(access, params)
        snapshot = None if plan is None else plan.value(checkpoint=self.workspace.check_deadline)
        self.plans = {
            k: v
            for k, v in self.plans.items()
            if v["deadline"] > self.engine.monotonic() and v["expires"] > self.engine.clock()
        }
        if (
            len(self.plans) >= 32
            or sum(v["owner"] == session.id for v in self.plans.values()) >= 8
            or sum(len(v["plan"].content) for v in self.plans.values() if v["plan"] is not None)
            + (len(plan.content) if plan is not None else 0)
            > 67_108_864
        ):
            raise DeskWeaveError("LIMIT_EXCEEDED", "运行准备计划数量或内容容量已满。")
        identity, expires = uuid.uuid4().hex, self.engine.clock() + 60
        requirements, problems = report["requirements"], report["diagnostics"]
        source = {
            "plan": snapshot,
            "workflows": report["workflows"],
            "version": plan.sha256 if plan is not None else digest(params),
        }
        queries = self.engine.run_queries
        required = queries.collection(
            session,
            "runRequirements",
            len(requirements),
            expires=expires,
            items=requirements,
            **source,
        )
        try:
            diagnostics = queries.collection(
                session, "diagnostics", len(problems), expires=expires, items=problems, **source
            )
        except BaseException:
            queries.close(session.id, required["collectionId"])
            raise
        self.plans[identity] = {
            "owner": session.id,
            "plan": plan,
            "expires": expires,
            "deadline": self.engine.monotonic() + 60,
            "binding": digest(self.engine.runs.planner._parameters(params)),
        }
        return {
            "planId": identity,
            "valid": plan is not None,
            "expiresAt": utc_time(expires),
            "definitionHash": report["definitionHash"],
            "requirementsQuery": required,
            "diagnosticsQuery": diagnostics,
            "requirementCount": len(requirements),
            "diagnosticCount": len(problems),
        }

    def start(self, session, params, request_id):
        def resolve(identity, normalized):
            item = self.plans.get(identity)
            if (
                item is None
                or item["owner"] != session.id
                or item["deadline"] <= self.engine.monotonic()
                or item["expires"] <= self.engine.clock()
            ):
                raise DeskWeaveError("RUN_PLAN_EXPIRED", "运行准备计划已过期，请重新准备。")
            if item["binding"] != digest({k: v for k, v in normalized.items() if k != "planId"}):
                raise DeskWeaveError("RUN_PLAN_STALE", "启动参数已变化，请重新准备。")
            if item["plan"] is None:
                raise DeskWeaveError("INVALID_WORKFLOW", "运行准备存在未满足的前置要求。")
            return item["plan"]

        return self.engine.runs.start(
            self.engine.run_access(session), request_id, params, plan_provider=resolve
        )

    def control(self, session, method, params, request_id, deadline, authorize):
        if "resumePlanId" in params or "confirmationGrant" in params:
            raise DeskWeaveError("METHOD_UNAVAILABLE", "当前执行端口不支持恢复确认计划。")
        access = self.engine.run_access(session)
        receipt = RunCommand(
            access,
            request_id,
            method,
            params,
            deadline=deadline,
            clock=self.engine.monotonic,
            authorize=authorize,
        )
        return self.engine.runs.runtime.control(
            params["runId"],
            method.split(".")[1],
            params["expectedControlVersion"],
            access=access,
            receipt=receipt,
        )
