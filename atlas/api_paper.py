"""Authenticated, paper-only owner review. Selection is never a native Start command."""
import os

from fastapi import APIRouter, Depends, HTTPException, Request

from atlas.api_handoff import _body, _decode_bundle, _get_config
from atlas.handoff import BundleError
from atlas.paper_state import store_for


def _paper_role():
    if os.environ.get("ATLAS_FREQTRADE_ROLE") != "paper":
        raise HTTPException(status_code=403, detail="Selection changes require the isolated paper role.")


async def _get_rpc():
    from freqtrade.rpc.api_server.deps import get_rpc
    async for rpc in get_rpc():
        yield rpc


def native_flat():
    """Actual persisted records, not /status (which can hide RPC errors as [])."""
    from freqtrade.persistence import Order, Trade
    return not Trade.get_open_trades() and not Order.get_open_orders()


def selection(store, rpc):
    return store.public(pid=os.getpid(), runtime_state=rpc._freqtrade.state.name.lower())


router = APIRouter(dependencies=[Depends(_paper_role)])


@router.get("/atlas/paper/selection")
def read_selection(config=Depends(_get_config), rpc=Depends(_get_rpc)):
    try:
        return selection(store_for(config["user_data_dir"]), rpc)
    except (BundleError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Paper selection journal is unreadable.") from exc


@router.post("/atlas/paper/stage", status_code=201)
async def stage(request: Request, config=Depends(_get_config), rpc=Depends(_get_rpc)):
    body = await _body(request)
    try:
        if set(body) != {"manifest", "files", "reviewed_sha256", "backtest"}:
            raise BundleError("Stage requires manifest, files, reviewed_sha256 and backtest evidence.")
        evidence = body.pop("backtest")
        bundle = _decode_bundle(body)
        store = store_for(config["user_data_dir"])
        store.stage(bundle, body["reviewed_sha256"], evidence)
        return selection(store, rpc)
    except (BundleError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc) if isinstance(exc, BundleError) else "Paper staging failed.") from exc


async def change(request, config, rpc, action):
    body = await _body(request, 4096)
    key = "reviewed_sha256" if action == "activate" else "expected_current_sha256"
    if set(body) != {key}:
        raise HTTPException(status_code=400, detail=f"Expected only {key}.")
    from freqtrade.enums import State
    bot = rpc._freqtrade
    store = store_for(config["user_data_dir"])
    try:
        with bot._exit_lock, store.lock:
            if not native_flat():
                raise HTTPException(status_code=409, detail="Open paper trades or orders prevent a strategy change. Nothing was closed or reset.")
            store.request(action, body[key], bot.state.name.lower())
            bot.state = State.STOPPED
        return selection(store, rpc)
    except (BundleError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc) if isinstance(exc, BundleError) else "Paper change could not be recorded.") from exc


@router.post("/atlas/paper/activate", status_code=202)
async def activate(request: Request, config=Depends(_get_config), rpc=Depends(_get_rpc)):
    return await change(request, config, rpc, "activate")


@router.post("/atlas/paper/rollback", status_code=202)
async def rollback(request: Request, config=Depends(_get_config), rpc=Depends(_get_rpc)):
    return await change(request, config, rpc, "rollback")
