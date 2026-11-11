"""Docker screens."""

from html import escape

from moon_watcher.config import DOCKER_MONITORING
from moon_watcher.monitors.docker_monitor import LIST_FILTERS, STATUS_ICON, DockerMonitor, classify, filter_containers
from moon_watcher.services import Services
from moon_watcher.ui.common import PAGE_SIZE, Screen, back_btn, btn, keyboard, menu_btn, page_arg, pager
from moon_watcher.utils.paging import paginate


def _unavailable(svc: Services) -> Screen | None:
    if not DOCKER_MONITORING:
        return "🐳 Docker monitoring is disabled (DOCKER_MONITORING=false).", keyboard([menu_btn()])
    return None


def _snapshot(svc: Services) -> tuple[list[dict] | None, Screen | None]:
    off = _unavailable(svc)
    if off:
        return None, off
    infos = svc.docker.snapshot(with_stats=False)
    if infos is None:
        return None, (
            f"❌ <b>Cannot reach Docker</b>\n\n<code>{escape(svc.docker.last_error or '')}</code>\n\n"
            "Check that the socket is mounted and DOCKER_GID is correct.",
            keyboard([btn("🔄 Retry", "k", "primary")], [menu_btn()]),
        )
    return infos, None


def docker_menu(svc: Services) -> Screen:
    infos, error = _snapshot(svc)
    if error:
        return error
    counts = {key: len(filter_containers(infos, key)) for key in LIST_FILTERS}

    def filter_btn(key):
        icon, label, _ = LIST_FILTERS[key]
        style = "danger" if key == "p" and counts[key] else None
        return btn(f"{icon} {label} ({counts[key]})", f"kl:{key}:0", style)

    return DockerMonitor.format_summary(infos), keyboard(
        [filter_btn("p"), filter_btn("r")],
        [filter_btn("s"), filter_btn("a")],
        [menu_btn(), btn("🔄 Refresh", "k", "primary")],
    )


def _filter(args: list[str], index: int) -> str:
    key = args[index] if len(args) > index else "a"
    return key if key in LIST_FILTERS else "a"


def docker_list(svc: Services, args: list[str]) -> Screen:
    key, page = _filter(args, 0), page_arg(args, 1)
    infos, error = _snapshot(svc)
    if error:
        return error
    items = filter_containers(infos, key)
    chunk, page, pages = paginate(items, page, PAGE_SIZE)
    rows = [[btn(f"{STATUS_ICON[classify(i)]} {i['name'][:35]}", f"kc:{i['id']}:{key}:{page}")] for i in chunk]
    return DockerMonitor.format_list(chunk, key, page, pages, len(items)), keyboard(
        *rows,
        pager(f"kl:{key}", page, pages),
        [back_btn("k"), menu_btn(), btn("🔄 Refresh", f"kl:{key}:{page}", "primary")],
    )


def container(svc: Services, args: list[str]) -> Screen:
    cid, key, page = (args[0] if args else ""), _filter(args, 1), page_arg(args, 2)
    ctx = f"{key}:{page}"
    back = [back_btn(f"kl:{ctx}"), menu_btn()]
    off = _unavailable(svc)
    if off:
        return off
    info = svc.docker.get_container(cid)
    if info is None:
        return "❌ Container not found (it may have been removed or recreated).", keyboard(back)
    return DockerMonitor.format_detail(info), keyboard(
        [btn("🔄 Refresh", f"kc:{cid}:{ctx}", "primary"), btn("📜 Logs", f"kg:{cid}:{ctx}")],
        back,
    )


def container_logs(svc: Services, args: list[str]) -> Screen:
    cid, key, page = (args[0] if args else ""), _filter(args, 1), page_arg(args, 2)
    logs = svc.docker.get_logs(cid, 30) if DOCKER_MONITORING else None
    if logs is None:
        text = "❌ Could not read logs."
    elif not logs.strip():
        text = "📜 No logs."
    else:
        lines = [ln[:200] for ln in logs.strip().splitlines()]
        text = f"📜 <b>Last 30 log lines</b>\n\n<pre>{escape(chr(10).join(lines)[-3000:])}</pre>"
    return text, keyboard([back_btn(f"kc:{cid}:{key}:{page}"), menu_btn()])
