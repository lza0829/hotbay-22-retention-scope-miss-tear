from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from app.services import retention as svc

router = APIRouter(tags=["保留"])


class _NoExtra(BaseModel):
    # 全局只贯彻片库间整间挂载：任何显式分区键名单字段在接口层即被拒绝，不允许混用。
    model_config = ConfigDict(extra="forbid")


class RunIn(_NoExtra):
    policy_id: int
    mode: str = "连目录销毁"


class PolicyIn(_NoExtra):
    name: str
    max_age_days: int
    action: str
    vault_id: int


@router.get("/保留策略")
def policies():
    return svc.list_policies()


@router.post("/保留策略")
def create_policy(body: PolicyIn):
    return svc.create_policy(body.name, body.max_age_days, body.action, body.vault_id)


@router.get("/保留批次")
def batches():
    return svc.list_batches()


@router.get("/销毁流水")
def destroys():
    return svc.list_destroys()


@router.get("/保留预览/{policy_id}")
def preview(policy_id: int):
    """干跑：按挂载谓词给出将命中/将跳过集合，不落任何写操作。"""
    return svc.preview_retention(policy_id)


@router.get("/保留重放/{batch_id}")
def replay(batch_id: int):
    """由批次挂载谓词重放命中集合，并与批次实际动作对账。"""
    return svc.replay_batch(batch_id)


@router.post("/保留执行")
def run(body: RunIn):
    return svc.run_retention(body.policy_id, body.mode)
