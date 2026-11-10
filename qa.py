"""Interactive-desktop integration QA using only an owned, offline fixture window."""
from pathlib import Path
import json
import time
import traceback

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QImage, QPainter, QColor, QPen
from PySide6.QtWidgets import QWidget

import native
from branding import APP_NAME


def run_self_test(controller, application):
    from app import RESOURCE_ROOT, APP_ROOT
    report_path = APP_ROOT / "qa-result.json"
    image = QImage(str(RESOURCE_ROOT / "samples" / "web-fixture.png"))
    if image.isNull():
        raise RuntimeError("QA fixture is missing")

    class Fixture(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(f"{APP_NAME} · 离线测试画面")
            self.resize(1230, round(1230 * image.height() / image.width()))
            self.move(40, 80)
            self.counter = 0
            self.clicks = []
            self.hover_card = False
            self.purchased_card = False
            self.empty_shop = False
            self.animation = QTimer(self)
            self.animation.timeout.connect(self.animate)
            self.animation.start(160)

        def animate(self):
            self.counter += 1
            self.update()

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.drawImage(self.rect(), image)
            def area(left, top, right, bottom):
                return QRectF(left*self.width(), top*self.height(),
                              (right-left)*self.width(), (bottom-top)*self.height())
            card = area(.332, .495, .464, .984)
            if self.hover_card:
                painter.fillRect(card, QColor(80, 130, 190, 18 if self.counter % 2 else 34))
                painter.setPen(QPen(QColor(155, 220, 250), 2))
                painter.drawRect(card)
            if self.purchased_card:
                painter.fillRect(card, QColor(15, 24, 20))
            if self.empty_shop:
                painter.fillRect(area(.332, .495, .863, .984), QColor(15, 24, 20))
            painter.fillRect(0, 0, 4, 4, QColor(20 + self.counter % 80, 90, 40))

        def mousePressEvent(self, event):
            self.clicks.append([event.position().x(), event.position().y()])

    fixture = Fixture()
    fixture.show()
    fixture.raise_()
    fixture.activateWindow()
    controller._qa_fixture = fixture
    state = {"phase": 0, "started": time.monotonic(), "checks": {}, "mode": "offline-owned-fixture"}
    timer = QTimer(controller)
    controller._qa_timer = timer
    timer.setInterval(180)

    def finish(error=None):
        timer.stop()
        state["status"] = "failed" if error else "passed"
        if error:
            state["error"] = str(error)
        state["duration_seconds"] = round(time.monotonic()-state["started"], 2)
        state["diagnostics"] = controller.diagnostics
        state["overlay_native"] = controller.overlay.native_status
        if controller.last_observation:
            state["last_observation"] = controller.last_observation
        report_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        controller.stop_scan()
        fixture.close()
        controller.close()

    def finish_ui_checks():
        controller.open_editor()
        controller.editor.grab().save(str(APP_ROOT / "qa-editor.png"))
        controller.editor.close()
        if not controller.scanning:
            raise RuntimeError("Closing editor stopped scanning")
        state["checks"]["editor_close_keeps_scanning"] = True
        controller.toggle_fold()
        state["checks"]["controller_collapses"] = controller.collapsed and not controller.body.isVisible()
        controller.stop_scan()
        if controller.overlay.isVisible() or controller.matches:
            raise RuntimeError("Stop left stale overlay")
        state["checks"]["stop_clears_overlay"] = True
        calibration = json.loads(controller.calibration_path.read_text(encoding="utf-8"))
        state["checks"]["calibration_persisted"] = bool(calibration["profiles"])
        finish()

    def step():
        try:
            now = time.monotonic()
            observation = controller.last_observation
            if observation and observation.get("sequence") != state.get("last_observed_sequence"):
                state["last_observed_sequence"] = observation.get("sequence")
                state.setdefault("recognition_timings", []).append({
                    "elapsed_ms": observation.get("elapsed_ms"),
                    "path": observation.get("diagnostics", {}).get("path"),
                    "frame_stable": observation.get("frame_stable"),
                    "job_elapsed_ms": observation.get("job_elapsed_ms"),
                    "passes": observation.get("recognition_passes", []),
                })
            if now-state["started"] > 120:
                raise RuntimeError("Timed out waiting for live capture/reminders: " + controller.status.text())
            phase = state["phase"]
            if phase == 0 and now-state["started"] > .5:
                controller.store.import_json((RESOURCE_ROOT / "samples" / "web-demo.json").read_text(encoding="utf-8"))
                controller.refresh_presets()
                controller.bind_target(int(fixture.winId()))
                geometry = native.get_window_geometry(int(fixture.winId()))
                rectangle = geometry["client_screen_rect"]
                controller.apply_calibration([.22, .47, .98, .99], [rectangle[2]-rectangle[0], rectangle[3]-rectangle[1]])
                controller.start_scan()
                state["phase"] = 1
            elif phase == 1 and len(controller.matches) >= 4 and controller.overlay.isVisible():
                state["first_reminder_ms_including_model_start_and_binding"] = round((now-state["started"])*1000, 1)
                state["checks"]["wgc_to_ocr_to_four_reminders"] = True
                state["recognized_names"] = [row["name"] for row in controller.matches]
                status = controller.overlay.native_status
                for flag in ("click_through", "layered", "no_activate"):
                    if not status.get(flag):
                        raise RuntimeError("Overlay flag missing: " + flag)
                state["checks"]["native_transparent_noactivate_flags"] = True
                controller.overlay.grab().save(str(APP_ROOT / "qa-overlay.png"))
                controller.grab().save(str(APP_ROOT / "qa-controller.png"))
                fixture.move(fixture.x()+65, fixture.y()+35)
                state["moved_at"] = now
                if (APP_ROOT / "qa-require-click.flag").exists():
                    state["phase"] = 4
                    state["waiting_for_click"] = True
                    (APP_ROOT / "qa-progress.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                else:
                    state["phase"] = 2
            elif phase == 4 and fixture.clicks:
                state["checks"]["os_click_delivered_through_overlay"] = True
                state["fixture_clicks"] = fixture.clicks
                state["moved_at"] = now
                state["phase"] = 2
            elif phase == 2 and now-state["moved_at"] > .7:
                target = native.get_window_geometry(int(fixture.winId()))["client_screen_rect"]
                overlay = native.get_window_geometry(int(controller.overlay.winId()))["client_screen_rect"]
                if tuple(target) != tuple(overlay):
                    raise RuntimeError(f"Move follow mismatch: {target} / {overlay}")
                state["checks"]["follows_window_move"] = True
                fixture.resize(1100, round(1100*image.height()/image.width()))
                state["resized_at"] = now
                state["phase"] = 3
            elif phase == 3 and now-state["resized_at"] > 4 and controller.overlay.isVisible():
                target = native.get_window_geometry(int(fixture.winId()))["client_screen_rect"]
                overlay = native.get_window_geometry(int(controller.overlay.winId()))["client_screen_rect"]
                if tuple(target) != tuple(overlay):
                    raise RuntimeError(f"Resize follow mismatch: {target} / {overlay}")
                state["checks"]["follows_proportional_resize"] = True
                state["stable_anchors"] = {row["name"]: row["anchor_norm"] for row in controller.matches}
                fixture.hover_card = True
                state["hover_started"] = now
                state["phase"] = 5
            elif phase == 5:
                if {row["name"] for row in controller.matches} != set(state["stable_anchors"]):
                    raise RuntimeError("Continuous synthetic hover cleared a confirmed reminder")
                if any(row["anchor_norm"] != state["stable_anchors"][row["name"]] for row in controller.matches):
                    raise RuntimeError("OCR jitter moved a locked marker")
                if now-state["hover_started"] > 3.5:
                    state["checks"]["continuous_hover_preserves_names_and_anchors"] = True
                    fixture.hover_card = False
                    fixture.purchased_card = True
                    state["purchase_started"] = now
                    state["phase"] = 6
            elif phase == 6:
                names = {row["name"] for row in controller.matches}
                others = set(state["stable_anchors"]) - {"野鬃"}
                if not others.issubset(names):
                    raise RuntimeError("Buying one card cleared an unrelated reminder")
                if any(row["anchor_norm"] != state["stable_anchors"][row["name"]] for row in controller.matches):
                    raise RuntimeError("Buying one card moved an unrelated marker")
                if "野鬃" not in names:
                    state.setdefault("purchase_removal_ms", round((now-state["purchase_started"])*1000, 1))
                    if now-state["purchase_started"] > 1:
                        state["checks"]["purchase_only_removes_affected_card"] = True
                        fixture.empty_shop = True
                        state["empty_started"] = now
                        state["phase"] = 7
                elif now-state["purchase_started"] > 4:
                    raise RuntimeError("Purchased card kept its old reminder")
            elif phase == 7 and not controller.matches:
                state["checks"]["real_name_disappearance_clears_old_markers"] = True
                fixture.empty_shop = fixture.purchased_card = False
                fixture.update()
                state["recovery_started"] = now
                state["refresh_recoveries"] = []
                state["phase"] = 8
            elif phase == 8:
                if len(controller.matches) == 4:
                    passes = (observation or {}).get("recognition_passes", [])
                    if any(item.get("path") == "full_shop_detection" for item in passes):
                        raise RuntimeError("Full-shop verification blocked the first post-refresh markers")
                    state["refresh_recoveries"].append({
                        "first_four_markers_ms": round((now-state["recovery_started"])*1000, 1),
                        "path": (observation or {}).get("diagnostics", {}).get("path"),
                        "passes": passes,
                    })
                    if len(state["refresh_recoveries"]) == 3:
                        state["checks"]["three_refresh_blanks_keep_name_layout"] = True
                        finish_ui_checks()
                    else:
                        fixture.empty_shop = True
                        fixture.update()
                        state["refresh_blank_started"] = now
                        state["phase"] = 9
                elif now-state["recovery_started"] > 5:
                    raise RuntimeError("Shop names did not recover after a short refresh blank")
            elif phase == 9 and now-state["refresh_blank_started"] > .24 and not controller.matches:
                fixture.empty_shop = False
                fixture.update()
                state["recovery_started"] = now
                state["phase"] = 8
        except Exception as error:
            state["traceback"] = traceback.format_exc()
            finish(error)

    timer.timeout.connect(step)
    timer.start()
