"""Main window: navigation, pages, banners and the wiring between UI and controllers."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeySequence, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from winstaller.core.catalog import Availability, CategoryKind, Item
from winstaller.core.runner import RunSummary, TaskResult, TaskStatus
from winstaller.core.updater import UpdateCheck
from winstaller.ui.bridge import (
    LogStore,
    RunController,
    RunState,
    ToolController,
    UpdateController,
    WingetController,
)
from winstaller.ui.icons import app_icon
from winstaller.ui.pages.about import AboutPage
from winstaller.ui.pages.log import LogPage
from winstaller.ui.pages.run import RunPage
from winstaller.ui.pages.selection import SelectionPage
from winstaller.ui.pages.tools import ToolsPage
from winstaller.ui.selection import SelectionModel
from winstaller.ui.services import ConfirmRequest, Services
from winstaller.ui.theme import ThemeManager, ThemeMode
from winstaller.ui.widgets.action_bar import ActionBar
from winstaller.ui.widgets.banner import Banner
from winstaller.ui.widgets.common import format_mb
from winstaller.ui.widgets.confirm_dialog import ask
from winstaller.ui.widgets.nav import NavRail

#: Install categories shown on the separate "System" page (everything else: "Aplikacje").
SYSTEM_CATEGORY_IDS = ("gpu-drivers", "system", "extras")

PAGES: list[tuple[str, str]] = [
    ("apps", "Aplikacje"),
    ("system", "System"),
    ("run", "Przebieg"),
    ("tools", "Narzędzia"),
    ("log", "Dziennik"),
    ("about", "O programie"),
]
SELECTION_PAGES = ("apps", "system")
SETTINGS_THEME_KEY = "ui/theme"


class MainWindow(QMainWindow):
    """Top-level window. All collaborators come from :class:`Services` (injectable).

    Test hooks: ``selection``, ``log``, ``run``, ``updates``, ``winget``, ``tools``
    (controllers); ``pages`` (id -> page), ``apps_page``, ``system_page``, ``run_page``,
    ``tools_page``, ``log_page``, ``about_page``; ``nav``, ``action_bar``,
    ``update_banner``, ``winget_banner``; methods ``navigate``, ``current_page_id``,
    ``start_install``, ``stop_run``, ``retry_failed``, ``back_to_selection``,
    ``start_background_tasks``. Signal ``pageChanged(page_id)``.
    """

    pageChanged = Signal(str)

    def __init__(
        self,
        services: Services,
        *,
        theme: ThemeManager | None = None,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.services = services
        self.settings = settings
        self.theme = theme or ThemeManager()
        self._close_requested = False
        self._force_close = False
        self._shown_once = False
        catalog = services.catalog

        self.setObjectName("mainWindow")
        title = f"Winstaller {services.version}"
        self.setWindowTitle(f"{title}  [DRY-RUN]" if services.dry_run else title)
        self.setWindowIcon(app_icon())
        self.resize(1100, 720)
        self.setMinimumSize(900, 600)

        # -- model and controllers
        tool_ids = {i.id for i in catalog.tools()}
        self.availability: dict[str, Availability] = {
            item.id: catalog.availability(item, arch=services.arch, base_dir=services.app_dir)
            for item in catalog.items()
        }
        self.selection = SelectionModel(
            catalog,
            lambda i: (
                i not in tool_ids and i in self.availability and self.availability[i].available
            ),
            self,
        )
        self.log = LogStore(parent=self)
        self.run = RunController(services, self.log, self)
        self.updates = UpdateController(services, self.log, self)
        self.winget = WingetController(services, self.log, self)
        self.tools = ToolController(services, self.log, self)

        # -- pages
        install_cats = catalog.categories(CategoryKind.INSTALL)
        apps_cats = [c for c in install_cats if c.id not in SYSTEM_CATEGORY_IDS]
        system_cats = [c for c in install_cats if c.id in SYSTEM_CATEGORY_IDS]
        self.apps_page = SelectionPage(
            page_id="apps",
            title="Aplikacje",
            subtitle="Zaznacz programy do zainstalowania — trafią do jednej kolejki.",
            categories=apps_cats,
            catalog=catalog,
            availability=self.availability,
            selection=self.selection,
            theme=self.theme,
            profiles=catalog.profiles(),
            arch=services.arch,
            search_placeholder="Szukaj programu…   Ctrl+F",
        )
        self.system_page = SelectionPage(
            page_id="system",
            title="System",
            subtitle=(
                "Sterowniki GPU, ustawienia Windows i pliki lokalne. "
                "Wykonywane w tej samej kolejce."
            ),
            categories=system_cats,
            catalog=catalog,
            availability=self.availability,
            selection=self.selection,
            theme=self.theme,
            arch=services.arch,
            search_placeholder="Szukaj ustawienia…   Ctrl+F",
        )
        self.apps_page.set_cross_page_counter(self.system_page.count_matches, "System")
        self.system_page.set_cross_page_counter(self.apps_page.count_matches, "Aplikacje")
        self.apps_page.jumpRequested.connect(lambda text: self._jump_search("system", text))
        self.system_page.jumpRequested.connect(lambda text: self._jump_search("apps", text))

        self.run_page = RunPage(self.log, self.theme)
        self.tools_page = ToolsPage(catalog.tools(), dry_run=services.dry_run)
        self.log_page = LogPage(self.log)
        self.about_page = AboutPage(
            version=services.version,
            arch=services.arch,
            dry_run=services.dry_run,
            item_count=len(catalog.items()),
            theme=self.theme,
            updates_enabled=self.updates.enabled,
        )
        self.pages: dict[str, QWidget] = {
            "apps": self.apps_page,
            "system": self.system_page,
            "run": self.run_page,
            "tools": self.tools_page,
            "log": self.log_page,
            "about": self.about_page,
        }

        # -- layout
        self.nav = NavRail(
            PAGES,
            self.theme,
            version=services.version,
            arch=services.arch.upper(),
            dry_run=services.dry_run,
        )
        self.nav.pageRequested.connect(self.navigate)
        self.stack = QStackedWidget()
        self.stack.setObjectName("pageStack")
        for page in self.pages.values():
            page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            page.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            self.stack.addWidget(page)
        self.update_banner = Banner("updateBanner")
        self.winget_banner = Banner("wingetBanner")
        self.action_bar = ActionBar()
        self.action_bar.installRequested.connect(self.start_install)
        self.action_bar.clearRequested.connect(self.selection.clear)
        self.action_bar.showRunRequested.connect(lambda: self.navigate("run"))

        right = QWidget()
        right.setObjectName("contentColumn")
        rlay = QVBoxLayout(right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(0)
        rlay.addWidget(self.update_banner)
        rlay.addWidget(self.winget_banner)
        rlay.addWidget(self.stack, 1)
        rlay.addWidget(self.action_bar)

        central = QWidget()
        central.setObjectName("root")
        clay = QVBoxLayout(central)
        clay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.nav)
        row.addWidget(right, 1)
        clay.addLayout(row)
        self.setCentralWidget(central)

        # -- wiring
        self.selection.changed.connect(self._refresh_selection_ui)
        self.selection.lockedChanged.connect(lambda _l: self._refresh_selection_ui())
        self.run.stateChanged.connect(self._on_run_state)
        self.run.runStarted.connect(self._on_run_started)
        self.run.taskStarted.connect(self._on_task_started)
        self.run.taskFinished.connect(self._on_task_finished)
        self.run.runFinished.connect(self._on_run_finished)
        self.run.runFailed.connect(self._on_run_failed)
        self.run_page.stopRequested.connect(self.stop_run)
        self.run_page.retryRequested.connect(self.retry_failed)
        self.run_page.backRequested.connect(self.back_to_selection)
        self.run_page.browseRequested.connect(lambda: self.navigate("apps"))

        self.tools_page.runRequested.connect(self.run_tool)
        self.tools_page.openRequested.connect(self._open_path)
        self.tools.toolStarted.connect(lambda i: self.tools_page.set_busy(i, True))
        self.tools.toolFinished.connect(self._on_tool_finished)

        self.about_page.themeRequested.connect(self.set_theme_mode)
        self.about_page.checkRequested.connect(lambda: self.updates.check(manual=True))
        self.about_page.openUrlRequested.connect(self._open_url)
        self.theme.changed.connect(lambda _p: self._refresh_run_badge())

        self.updates.checking.connect(self._on_update_checking)
        self.updates.checkFinished.connect(self._on_update_checked)
        self.updates.checkFailed.connect(self._on_update_failed)
        self.updates.downloadStarted.connect(self._on_download_started)
        self.updates.downloadProgress.connect(self._on_download_progress)
        self.updates.downloadFinished.connect(self._on_download_finished)
        self.updates.downloadFailed.connect(self._on_download_failed)
        self.updates.downloadCancelled.connect(self._show_update_available)
        self.updates.applied.connect(self._quit_for_update)

        self.winget.availabilityChecked.connect(self._on_winget_checked)
        self.winget.installStarted.connect(self._on_winget_installing)
        self.winget.installFinished.connect(self._on_winget_installed)

        self._install_shortcuts()
        self.navigate("apps")
        self._refresh_selection_ui()
        self.theme.apply()

    # ------------------------------------------------------------ lifecycle
    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.theme.track_window(self)
        if not self._shown_once:
            self._shown_once = True
            # Start where the user acts first: typing filters immediately.
            self.apps_page.search.setFocus(Qt.FocusReason.OtherFocusReason)

    def start_background_tasks(self) -> None:
        """Deferred start-up work (after the window is shown): nothing here blocks."""
        self.log.info(
            f"Winstaller {self.services.version} · {self.services.arch} · "
            + ("tryb dry-run" if self.services.dry_run else "tryb normalny")
        )
        self.updates.cleanup_old()
        if self.services.auto_check_updates and self.updates.enabled:
            self.updates.check(manual=False)
        if self.services.check_winget_on_start:
            self.winget.check()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._force_close or not self.run.busy:
            if self.updates.downloading:
                self.updates.cancel_download()
            self.winget.cancel()
            event.accept()
            return
        if self._close_requested:
            event.ignore()
            return
        ok = self._confirm(
            ConfirmRequest(
                title="Przerwać instalację i zamknąć?",
                message=(
                    "Kolejka jest w toku. Bieżący proces zostanie zatrzymany, a pozostałe "
                    "zadania anulowane. Okno zamknie się po zatrzymaniu."
                ),
                accept_text="Zatrzymaj i zamknij",
                reject_text="Kontynuuj instalację",
            )
        )
        event.ignore()
        if ok:
            self._close_requested = True
            self.stop_run()

    # ------------------------------------------------------------ navigation
    def current_page_id(self) -> str:
        current = self.stack.currentWidget()
        return next(pid for pid, page in self.pages.items() if page is current)

    def navigate(self, page_id: str) -> None:
        page = self.pages[page_id]
        old_page = self.stack.currentWidget()
        before = QApplication.focusWidget()
        self.stack.setCurrentWidget(page)
        self.nav.set_current(page_id)
        self.action_bar.setVisible(page_id in SELECTION_PAGES)
        if before is None or before is old_page or old_page.isAncestorOf(before):
            # Otherwise Qt hands focus to the first focusable widget of the new page,
            # which shows a keyboard focus ring nobody asked for.
            page.setFocus(Qt.FocusReason.OtherFocusReason)
        self._refresh_selection_ui()
        self.pageChanged.emit(page_id)

    def _jump_search(self, page_id: str, text: str) -> None:
        self.navigate(page_id)
        page = self.pages[page_id]
        assert isinstance(page, SelectionPage)
        page.set_filter(text)
        page.focus_search()

    def _install_shortcuts(self) -> None:
        def add(seq: QKeySequence | str, slot) -> None:  # type: ignore[no-untyped-def]
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

        add(QKeySequence.StandardKey.Find, self.focus_search)
        add("Ctrl+Return", self._shortcut_install)
        add("Ctrl+Enter", self._shortcut_install)
        for n, (page_id, _t) in enumerate(PAGES, start=1):
            add(f"Ctrl+{n}", lambda pid=page_id: self.navigate(pid))

    def focus_search(self) -> None:
        page_id = self.current_page_id()
        if page_id not in SELECTION_PAGES:
            page_id = "apps"
            self.navigate(page_id)
        page = self.pages[page_id]
        assert isinstance(page, SelectionPage)
        page.focus_search()

    def _shortcut_install(self) -> None:
        if self.current_page_id() in SELECTION_PAGES and self.action_bar.install_button.isEnabled():
            self.start_install()

    # ------------------------------------------------------------- selection
    def _refresh_selection_ui(self) -> None:
        total = self.selection.count()
        apps = self.apps_page.selected_count()
        system = self.system_page.selected_count()
        page_id = self.current_page_id() if self.stack.count() else "apps"
        other = system if page_id == "apps" else apps if page_id == "system" else 0
        self.action_bar.update_state(total, busy=self.run.busy, on_other_pages=other)
        self.nav.set_badge("apps", str(apps) if apps else "", "accent")
        self.nav.set_badge("system", str(system) if system else "", "accent")

    # ------------------------------------------------------------------ run
    def _confirm(self, request: ConfirmRequest) -> bool:
        fn = self.services.confirm
        return fn(self, request) if fn is not None else ask(self, request)

    def _confirm_destructive(self, items: list[Item]) -> bool:
        risky = [i for i in items if i.destructive]
        if not risky:
            return True
        n = len(risky)
        return self._confirm(
            ConfirmRequest(
                title="Potwierdź operacje systemowe" if n > 1 else "Potwierdź operację systemową",
                message=(
                    f"Kolejka zawiera {n} "
                    + ("pozycję, która zmienia" if n == 1 else "pozycje, które zmieniają")
                    + " system. Pozostałe pozycje nie wymagają potwierdzenia."
                ),
                details=tuple((i.name, i.confirm or "") for i in risky),
                accept_text="Kontynuuj",
                reject_text="Anuluj",
            )
        )

    def start_install(self, item_ids: list[str] | None = None) -> bool:
        """Start the queue for ``item_ids`` (default: the current selection)."""
        if self.run.busy:
            return False
        ids = list(item_ids) if item_ids is not None else self.selection.ids()
        if not ids:
            return False
        catalog = self.services.catalog
        items = [catalog.item(i) for i in catalog.in_catalog_order(ids)]
        if not self._confirm_destructive(items):
            self.log.info("Start kolejki anulowany przez użytkownika.")
            return False
        if not self.run.start([i.id for i in items]):
            return False
        self.run_page.begin(items)
        self.log.info(f"Start kolejki: {len(items)} pozycji.")
        self.navigate("run")
        self._refresh_run_badge()
        return True

    def stop_run(self) -> None:
        if self.run.cancel():
            self.run_page.set_stopping()

    def retry_failed(self) -> bool:
        summary = self.run.last_summary
        if summary is None or self.run.busy:
            return False
        return self.start_install(summary.failed_ids)

    def back_to_selection(self) -> None:
        self.navigate("apps")

    def _on_run_state(self, state: RunState) -> None:
        self.selection.set_locked(self.run.busy)
        self.tools_page.set_locked(self.run.busy)
        self._refresh_selection_ui()
        self._refresh_run_badge()

    def _on_run_started(self, _tasks: object) -> None:
        self.run_page.set_running()

    def _on_task_started(self, item_id: str) -> None:
        self.run_page.task_started(item_id)
        self._refresh_run_badge()

    def _on_task_finished(self, result: TaskResult) -> None:
        self.run_page.task_finished(result)
        self._refresh_run_badge()

    def _on_run_finished(self, summary: RunSummary) -> None:
        self.run_page.finish(summary)
        ok_ids = [r.item_id for r in summary.results if r.status == TaskStatus.OK]
        # Done items leave the selection; failed/cancelled stay selected for another try.
        self.selection.deselect_many(ok_ids)
        self.log.info(
            f"Koniec kolejki: OK {summary.ok_count}, błędy {summary.failed_count}, "
            f"pominięte {summary.skipped_count}, anulowane {summary.cancelled_count}."
        )
        self._refresh_run_badge()
        if self._close_requested:
            self._force_close = True
            QTimer.singleShot(0, self.close)

    def _on_run_failed(self, message: str) -> None:
        self.run_page.current_label.setText("Nie udało się uruchomić kolejki")
        self.run_page.status_label.setText(message)
        if self._close_requested:
            self._force_close = True
            QTimer.singleShot(0, self.close)

    def _refresh_run_badge(self) -> None:
        model = self.run_page.model
        total = model.rowCount()
        if not total:
            self.nav.set_badge("run", "")
            return
        done = total - model.count(TaskStatus.PENDING, TaskStatus.RUNNING)
        failed = model.count(TaskStatus.FAILED)
        if self.run.busy:
            self.nav.set_badge("run", f"{done}/{total}", "accent")
        elif failed:
            self.nav.set_badge("run", f"{failed} BŁ.", "error")
        else:
            self.nav.set_badge("run", f"{done}/{total}", "muted")

    # ---------------------------------------------------------------- tools
    def run_tool(self, item_id: str) -> bool:
        if self.run.busy or self.tools.is_running(item_id):
            return False
        item = self.services.catalog.item(item_id)
        if item.destructive and not self._confirm(
            ConfirmRequest(
                title=item.name,
                message=item.confirm,
                accept_text=self.tools_page.cards[item_id].action_text,
            )
        ):
            return False
        self.log.info(f"Narzędzie: {item.name}")
        return self.tools.run(item_id)

    def _on_tool_finished(self, item_id: str, result: TaskResult) -> None:
        self.tools_page.set_busy(item_id, False)
        self.tools_page.show_result(item_id, result)

    def _open_path(self, path: Path | None) -> None:
        if path is None:
            return
        if self.services.open_path is not None:
            self.services.open_path(path)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_url(self, url: str) -> None:
        if self.services.open_url is not None:
            self.services.open_url(url)
        else:
            QDesktopServices.openUrl(QUrl(url))

    # ---------------------------------------------------------------- theme
    def set_theme_mode(self, mode: ThemeMode) -> None:
        self.theme.set_mode(mode)
        self.about_page.set_theme_mode(mode)
        if self.settings is not None:
            self.settings.setValue(SETTINGS_THEME_KEY, ThemeMode(mode).value)

    # -------------------------------------------------------------- updates
    def _on_update_checking(self, manual: bool) -> None:
        if manual:
            self.about_page.set_update_status("Sprawdzanie…", busy=True)

    def _on_update_checked(self, result: UpdateCheck, manual: bool) -> None:
        if result.update_available:
            self.about_page.set_update_status(
                f"Dostępna wersja {result.latest} (masz {result.current}).", tone="accent"
            )
            self._show_update_available()
        elif manual:
            self.about_page.set_update_status(
                f"Masz najnowszą wersję ({result.current}).", tone="muted"
            )

    def _on_update_failed(self, message: str, manual: bool) -> None:
        if manual:
            self.about_page.set_update_status(message, tone="error")

    def _show_update_available(self) -> None:
        check = self.updates.last_check
        if check is None:
            return
        self.update_banner.configure(
            tag="[AKTUALIZACJA]",
            message=f"Dostępna wersja {check.latest}",
            detail=f"Masz {check.current}"
            + (
                " · tryb dry-run: pobieranie zostanie zasymulowane"
                if self.updates.simulated
                else ""
            ),
            actions=[
                ("Aktualizuj", "primary", self.start_update, "updateNow"),
                ("Później", "ghost", self.update_banner.hide, "updateLater"),
            ],
        )

    def start_update(self) -> bool:
        if self.run.busy:
            self.update_banner.set_detail("Najpierw zakończ albo zatrzymaj instalację.")
            return False
        return self.updates.download()

    def _on_download_started(self) -> None:
        check = self.updates.last_check
        version = check.latest if check else ""
        self.update_banner.configure(
            tag="[AKTUALIZACJA]",
            message=f"Pobieranie wersji {version}…",
            detail="",
            actions=[("Anuluj", "secondary", self.updates.cancel_download, "updateCancel")],
            progress=(0, -1),
        )

    def _on_download_progress(self, received: int, total: int) -> None:
        self.update_banner.set_progress((received, total))
        detail = format_mb(received) + (f" / {format_mb(total)}" if total > 0 else "")
        self.update_banner.set_detail(detail)

    def _on_download_finished(self, path: Path) -> None:
        if self.updates.simulated:
            self.update_banner.configure(
                tag="[DRY-RUN]",
                message="Pobieranie zasymulowane — plik programu nie został podmieniony.",
                actions=[("Zamknij", "ghost", self.update_banner.hide, "updateDismiss")],
            )
        else:
            self.update_banner.configure(
                tag="[AKTUALIZACJA]",
                message="Uruchamianie nowej wersji…",
                progress=None,
                busy=True,
            )

    def _on_download_failed(self, message: str) -> None:
        self.update_banner.configure(
            tag="[BŁĄD]",
            tone="error",
            message="Nie udało się zaktualizować programu.",
            detail=message,
            actions=[
                ("Spróbuj ponownie", "secondary", self.start_update, "updateRetry"),
                ("Zamknij", "ghost", self.update_banner.hide, "updateDismiss"),
            ],
        )

    def _quit_for_update(self) -> None:
        self._force_close = True
        self.close()
        QApplication.quit()

    # --------------------------------------------------------------- winget
    def _on_winget_checked(self, available: bool) -> None:
        if available:
            self.winget_banner.hide()
            return
        self.winget_banner.configure(
            tag="[WINGET]",
            tone="error",
            message="Nie znaleziono winget — pozycje [WINGET] nie zainstalują się.",
            detail="Winstaller może zarejestrować App Installer albo pobrać winget z GitHub.",
            actions=[
                ("Zainstaluj winget", "primary", self.winget.install, "wingetInstall"),
                ("Ukryj", "ghost", self.winget_banner.hide, "wingetHide"),
            ],
        )

    def _on_winget_installing(self) -> None:
        self.winget_banner.configure(
            tag="[WINGET]",
            message="Instalowanie winget…",
            detail="Szczegóły w Dzienniku.",
            actions=[("Dziennik  →", "ghost", lambda: self.navigate("log"), "wingetLog")],
            busy=True,
        )

    def _on_winget_installed(self, ok: bool, message: str) -> None:
        if ok:
            self.winget_banner.configure(
                tag="[OK]",
                message=message,
                actions=[("Zamknij", "ghost", self.winget_banner.hide, "wingetHide")],
            )
            QTimer.singleShot(6000, self.winget_banner.hide)
        else:
            self.winget_banner.configure(
                tag="[WINGET]",
                tone="error",
                message="Nie udało się zainstalować winget.",
                detail=message,
                actions=[
                    ("Spróbuj ponownie", "secondary", self.winget.install, "wingetInstall"),
                    ("Dziennik  →", "ghost", lambda: self.navigate("log"), "wingetLog"),
                ],
            )
