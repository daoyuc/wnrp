# -*- coding: utf-8 -*-
"""整套服务编排：一键启动 / 停止（各 PHP + Redis + [MySQL] + Nginx）。

顺序：
- 启动：PHP → Redis → MySQL → Nginx（先备好后端，最后开入口）；
- 停止：Nginx → PHP → Redis → MySQL（先关入口，再停后端）。

PHP 版本的启动范围（`php_scope`，见 PHP_SCOPES）：
- newest（默认，开机自启用）：只启动版本号最新的那个，避免登录时一次拉起全部版本；
- used：站点（nginx vhost）实际引用到的版本 + 最新版；
- active：跟随 cmd / 终端中 `php` 实际生效的版本（找不到则回落最新）；
- custom：只启动 PHP 页勾选了「开机自启」的版本（见 SETTING_AUTOSTART_VERSIONS）；
- all：全部版本（旧的「全部启动」行为，GUI 一键启动/CLI 默认仍用它）。
手动一键启动与停止始终覆盖全部 PHP（停止不受 scope 限制）。

每项独立 try/except，单项失败不影响后续，最终汇总为多行文本返回。

每项的成败都会写入全局运行日志（core.run_log），供「运行日志」页签查看。
"""
import os

from . import run_log
from .i18n import t
from .php_manager import version_key

# PHP 启动范围（界面下拉的顺序即此顺序：默认项在最前）
PHP_SCOPE_NEWEST = "newest"
PHP_SCOPE_USED = "used"
PHP_SCOPE_ACTIVE = "active"
PHP_SCOPE_CUSTOM = "custom"
PHP_SCOPE_ALL = "all"
PHP_SCOPES = (PHP_SCOPE_NEWEST, PHP_SCOPE_USED, PHP_SCOPE_ACTIVE,
              PHP_SCOPE_CUSTOM, PHP_SCOPE_ALL)

#: 开机自启勾选名单的配置键（PHP 页「开机自启」列维护；scope=custom 时生效）
SETTING_AUTOSTART_VERSIONS = "autostart_php_versions"

_SCOPE_LABELS = {
    PHP_SCOPE_NEWEST: "仅最新版本",
    PHP_SCOPE_USED: "站点实际引用 + 最新版",
    PHP_SCOPE_ACTIVE: "跟随 cmd 中生效的版本",
    PHP_SCOPE_CUSTOM: "按 PHP 页勾选",
    PHP_SCOPE_ALL: "全部版本",
}


def scope_label(scope: str) -> str:
    """启动范围的展示名（经 i18n 翻译）；未知取值按默认的「仅最新版本」显示。"""
    return t(_SCOPE_LABELS.get(scope, _SCOPE_LABELS[PHP_SCOPE_NEWEST]))


def autostart_versions(config) -> list[str]:
    """设置里勾选的开机自启版本名（去空白、去重、保持配置顺序）。

    只接受字符串项：配置被手工改坏（非列表 / 含非字符串）时忽略非法项，
    保证「勾选框 → 配置 → 开机策略」这条链路不因脏数据抛错。
    """
    raw = config.get_setting(SETTING_AUTOSTART_VERSIONS, []) if config else []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out


def set_autostart_versions(config, names: list[str]) -> None:
    """写入勾选名单（去重保序，供 PHP 页勾选框与脚本共用同一份格式）。"""
    out: list[str] = []
    for item in names or []:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    config.set_setting(SETTING_AUTOSTART_VERSIONS, out)


def sync_crash_watch(names, watched: bool) -> None:
    """同步崩溃自愈守护进程（core.crash_watchdog）的看护名单。

    停止后**必须**解除看护：守护进程按「端口/进程失联」探测并自动拉起，若不同步
    名单，一键「全部停止」后 PHP 会被守护进程立刻拉回（真实故障：总览点
    「全部停止」→ 十几秒后各版本重新上线）。启动则反向登记，让崩溃自愈继续覆盖
    这些版本。

    失败只记一条警告：看护名单同步不了不能影响启停主流程。
    """
    picked = [str(n) for n in (names or []) if str(n)]
    if not picked:
        return
    try:
        from . import crash_watchdog

        if watched:
            for name in picked:
                crash_watchdog.watch_version(name)
        else:
            crash_watchdog.unwatch_many(picked)
    except Exception as e:  # noqa: BLE001
        run_log.warn("services", t("同步崩溃自愈看护失败：{err}", err=e))


class ServiceGroup:
    """把各 manager 组合成「一键启停」。"""

    def __init__(self, php_mgr, nginx_mgr, redis_mgr, mysql_mgr=None):
        self.php_mgr = php_mgr
        self.nginx_mgr = nginx_mgr
        self.redis_mgr = redis_mgr
        self.mysql_mgr = mysql_mgr

    # ------------------------------------------------------------------ #
    def _scan_php(self) -> list:
        """确保 PHP 版本已扫描并刷新运行状态（失败时保持现状），返回版本列表。"""
        try:
            if not self.php_mgr.versions:
                # 首次调用时尚未扫描（如托盘还没打开过 PHP 页）
                self.php_mgr.versions = self.php_mgr.scan_versions()
            self.php_mgr.refresh_all_status()
        except Exception:  # noqa: BLE001
            pass
        return list(self.php_mgr.versions or [])

    def _refresh(self) -> None:
        """刷新各服务状态（避免重复启停已运行的服务）。"""
        self._scan_php()
        try:
            if self.redis_mgr is not None:
                self.redis_mgr.get_status_all()
        except Exception:  # noqa: BLE001
            pass
        if self.mysql_mgr is not None:
            for inst in self.mysql_mgr.instances:
                try:
                    self.mysql_mgr.get_status(inst)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ #
    # PHP 启动范围（按策略挑选本次要启动的版本）
    # ------------------------------------------------------------------ #
    def _ensure_display(self) -> None:
        """display 为空时先解析版本号（跑 `php -v`，带 mtime 缓存），供「最新版」判定。"""
        versions = self.php_mgr.versions or []
        if not any(not (getattr(v, "display", "") or "") for v in versions):
            return
        resolve = getattr(self.php_mgr, "resolve", None)
        if not callable(resolve):
            return
        try:
            resolve(refresh_status=False)
        except Exception:  # noqa: BLE001 —— 解析失败则用目录名兜底（见 version_key）
            pass

    def _newest(self):
        """版本号最新的版本（本机即 php85）；无版本时返回 None。"""
        versions = self.php_mgr.versions or []
        if not versions:
            return None
        self._ensure_display()
        return max(versions, key=version_key)

    def _active(self):
        """cmd / 终端中 `php` 实际生效的版本（User PATH 置顶者）；找不到回落最新。"""
        try:
            from .path_manager import get_effective_php_dir

            target = get_effective_php_dir()
        except Exception:  # noqa: BLE001
            target = None
        if target:
            key = os.path.normcase(os.path.abspath(target))
            for v in self.php_mgr.versions or []:
                if os.path.normcase(os.path.abspath(v.dir)) == key:
                    return v
        return self._newest()

    def _used(self):
        """站点实际引用到的版本（vhost 的 fastcgi_pass 端口反查）+ 最新版。"""
        picked = [self._newest()]
        ports: set[int] = set()
        try:
            from .vhost_manager import VhostManager

            vm = VhostManager(self.php_mgr.config)
            ports = {e.port for e in vm.scan() if e.port}
        except Exception:  # noqa: BLE001 —— 读不到 nginx 配置时退化为「仅最新」
            ports = set()
        for v in self.php_mgr.versions or []:
            if v.port in ports and v not in picked:
                picked.append(v)
        return picked

    def _custom(self):
        """PHP 页勾选了「开机自启」的版本；一个都没勾时回落「仅最新版本」。"""
        names = autostart_versions(self.php_mgr.config)
        if not names:
            run_log.warn("services", t("未勾选任何开机自启的 PHP 版本，已按「仅最新版本」处理"))
            return [self._newest()]
        wanted = set(names)
        picked = [v for v in self.php_mgr.versions or [] if v.name in wanted]
        missing = sorted(wanted - {v.name for v in picked})
        if missing:
            run_log.warn("services", t("未找到 PHP 版本：{names}",
                                       names="、".join(missing)))
        return picked or [self._newest()]

    def php_targets(self, php_scope: str = PHP_SCOPE_ALL,
                    php_names: list[str] | None = None) -> tuple[list, list]:
        """按策略挑出本次要启动的 PHP 版本，返回 (要启动, 被跳过)。

        - php_names 非空时按版本名精确指定（忽略 php_scope）；
        - custom 取 PHP 页勾选名单（未勾选则回落「仅最新版本」）；
        - 未知 php_scope 保守回落到「仅最新版本」并记日志；
        - 返回值按版本号升序排列，便于日志阅读。
        """
        versions = list(self.php_mgr.versions or [])
        if not versions:
            # 尚未扫描（如 CLI 的 --dry-run 直接调本方法）时先扫一次
            versions = self._scan_php()
        if not versions:
            return [], []
        scope = (php_scope or PHP_SCOPE_ALL).lower()

        if php_names:
            wanted = {str(n) for n in php_names}
            picked = [v for v in versions if v.name in wanted]
            missing = sorted(wanted - {v.name for v in picked})
            if missing:
                run_log.warn("services", t("未找到 PHP 版本：{names}",
                                           names="、".join(missing)))
        elif scope == PHP_SCOPE_ALL:
            return versions, []
        elif scope == PHP_SCOPE_NEWEST:
            picked = [self._newest()]
        elif scope == PHP_SCOPE_ACTIVE:
            picked = [self._active()]
        elif scope == PHP_SCOPE_USED:
            picked = self._used()
        elif scope == PHP_SCOPE_CUSTOM:
            picked = self._custom()
        else:
            run_log.warn("services", t("未知的 PHP 启动策略：{scope}，已按「仅最新版本」处理",
                                       scope=scope))
            picked = [self._newest()]

        kept = [v for v in picked if v is not None]
        ids = {id(v) for v in kept}
        skipped = [v for v in versions if id(v) not in ids]
        kept.sort(key=version_key)
        return kept, skipped

    @staticmethod
    def _verb(action: str, ok_: bool) -> str:
        """日志措辞：启动/停止 + 成功/失败（action 为空时退化为操作成功/失败）。"""
        table = {
            ("start", True): t("启动成功"), ("start", False): t("启动失败"),
            ("stop", True): t("停止成功"), ("stop", False): t("停止失败"),
        }
        return table.get((action, ok_), t("操作成功") if ok_ else t("操作失败"))

    def _call(self, tag: str, fn, *args, action: str = "", on_ok=None) -> str:
        """调用单项服务操作：异常不外抛先转文本，结果写入全局运行日志。

        「自动启动 / 一键启停」时逐项记录 PHP / Redis / MySQL / Nginx
        各自成功还是失败，便于事后在「运行日志」页签定位是哪一项没起来。

        ``on_ok``：仅在该项**成功**后执行（如同步崩溃自愈看护）；回调自身抛错
        只记一条警告，不影响该项结果与后续项。
        """
        try:
            msg = fn(*args)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}：{e}"
            run_log.error("services", f"{tag} {self._verb(action, False)}：{msg}")
        else:
            run_log.ok("services", f"{tag} {self._verb(action, True)}：{msg}")
            if on_ok is not None:
                try:
                    on_ok()
                except Exception as e:  # noqa: BLE001
                    run_log.warn("services", t("同步崩溃自愈看护失败：{err}", err=e))
        return f"{tag}：{msg}"

    # ------------------------------------------------------------------ #
    def start_all(self, php_scope: str = PHP_SCOPE_ALL,
                  php_names: list[str] | None = None) -> str:
        """启动整套服务；php_scope 决定启动哪些 PHP 版本（默认全部，兼容旧行为）。"""
        self._refresh()
        lines: list[str] = []

        targets, skipped = self.php_targets(php_scope, php_names)
        if targets:
            run_log.info("services", t("PHP 启动策略：{scope}（{names}）",
                                       scope=scope_label(php_scope),
                                       names="、".join(v.name for v in targets)))
        started: list[str] = []
        for v in targets:
            if getattr(v, "running", False):
                continue
            lines.append(self._call(f"PHP {v.name}", self.php_mgr.start, v, action="start",
                                    on_ok=lambda v=v: started.append(v.name)))
        sync_crash_watch(started, True)
        if skipped:
            # 明确写出「没启动哪些」，避免误判为启动失败
            lines.append(t("跳过 PHP 版本：{names}（策略：{scope}）",
                           names="、".join(v.name for v in skipped),
                           scope=scope_label(php_scope)))
        for inst in (self.redis_mgr.instances if self.redis_mgr else []):
            if getattr(inst, "running", False):
                continue
            lines.append(self._call(f"Redis {inst.name}", self.redis_mgr.start, inst,
                                    action="start"))
        if self.mysql_mgr is not None:
            for m in self.mysql_mgr.instances or []:
                if getattr(m, "running", False):
                    continue
                lines.append(self._call(f"MySQL {m.name}", self.mysql_mgr.start, m,
                                        action="start"))
        try:
            running, _ = self.nginx_mgr.get_status()
        except Exception:  # noqa: BLE001
            running = False
        if not running:
            lines.append(self._call("Nginx", self.nginx_mgr.start, action="start"))

        summary = "\n".join(lines) or t("所有服务均已在运行，无需启动")
        if not lines:
            run_log.info("services", summary)
        return summary

    def stop_all(self) -> str:
        """停止整套服务（始终覆盖全部 PHP，不受启动范围限制）。"""
        self._refresh()
        lines: list[str] = []

        try:
            running, _ = self.nginx_mgr.get_status()
        except Exception:  # noqa: BLE001
            running = False
        if running:
            lines.append(self._call("Nginx", self.nginx_mgr.stop, action="stop"))

        # 「全部停止」= 明确不要任何 PHP 运行：停止成功的与原本就没运行的，
        # 一律移出崩溃看护名单，否则守护进程会把它们逐个拉回（失联探测）。
        stopped: list[str] = []
        for v in self.php_mgr.versions or []:
            if not getattr(v, "running", False):
                stopped.append(v.name)
                continue
            lines.append(self._call(f"PHP {v.name}", self.php_mgr.stop, v, action="stop",
                                    on_ok=lambda v=v: stopped.append(v.name)))
        sync_crash_watch(stopped, False)
        for inst in (self.redis_mgr.instances if self.redis_mgr else []):
            if not getattr(inst, "running", False):
                continue
            lines.append(self._call(f"Redis {inst.name}", self.redis_mgr.stop, inst,
                                    action="stop"))
        if self.mysql_mgr is not None:
            for m in self.mysql_mgr.instances or []:
                if not getattr(m, "running", False):
                    continue
                lines.append(self._call(f"MySQL {m.name}", self.mysql_mgr.stop, m,
                                        action="stop"))

        summary = "\n".join(lines) or t("没有正在运行的服务")
        if not lines:
            run_log.info("services", summary)
        return summary
