"""Local window pixels -> shop name observations. No DOM or game process access.

Coordinates are normalized client-area [left, top, right, bottom] boundaries.
The capture buffer never leaves memory. Callbacks copy native frame memory before
returning; OCR stays in the caller's background worker and is not run on the
Windows Graphics Capture callback thread.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import importlib.metadata
import importlib.resources
from pathlib import Path
import threading
import time
import unicodedata
from typing import Iterable, Sequence

import numpy as np

from image_signature import build_name_signature, compare_name_signatures


class CaptureUnavailable(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class CapturedFrame:
    image_bgr: np.ndarray
    sequence: int
    timestamp: float  # time.monotonic(), not Unix time
    geometry: dict
    source: str = "windows_graphics_capture"


def _geometry(hwnd: int) -> dict:
    from native import get_window_geometry

    try:
        geometry = get_window_geometry(hwnd)
    except Exception as exc:
        raise CaptureUnavailable("window_unavailable", f"无法读取绑定窗口：{exc}") from exc
    if not geometry:
        raise CaptureUnavailable("window_closed", "绑定的窗口已关闭。")
    if geometry.get("minimized"):
        raise CaptureUnavailable("window_minimized", "窗口已最小化，识别已暂停。")
    if not geometry.get("visible", True) or geometry.get("cloaked", False):
        raise CaptureUnavailable("window_hidden", "绑定的窗口当前不可见，识别已暂停。")
    return geometry


def map_capture_to_client(image_bgr: np.ndarray, geometry: dict) -> np.ndarray:
    """Map a WGC buffer to physical client pixels, refusing unknown geometry.

    Different Windows/application builds can capture client pixels or extended
    frame bounds. Match the actual buffer size to an observed native rectangle;
    never guess an offset or stretch an unmatched frame to fit the client.
    """
    if image_bgr.ndim != 3 or image_bgr.shape[2] < 3:
        raise CaptureUnavailable("invalid_frame", "捕获画面格式无效。")
    height, width = image_bgr.shape[:2]
    client_rect = tuple(int(v) for v in geometry["client_screen_rect"])
    client_width = client_rect[2] - client_rect[0]
    client_height = client_rect[3] - client_rect[1]
    if min(client_width, client_height) <= 0:
        raise CaptureUnavailable("invalid_geometry", "窗口客户区没有有效尺寸。")
    if (width, height) == (client_width, client_height):
        return np.ascontiguousarray(image_bgr[:, :, :3])
    for key in ("frame_rect", "window_rect"):
        rect = geometry.get(key)
        if not rect:
            continue
        left, top, right, bottom = (int(v) for v in rect)
        if (right - left, bottom - top) != (width, height):
            continue
        x, y = client_rect[0] - left, client_rect[1] - top
        if x < 0 or y < 0 or x + client_width > width or y + client_height > height:
            continue
        return np.ascontiguousarray(image_bgr[y : y + client_height, x : x + client_width, :3])
    raise CaptureUnavailable(
        "geometry_mismatch",
        f"捕获尺寸 {width}×{height} 与窗口客户区不一致，请重新绑定窗口。",
    )


class WindowCapture:
    """Precise HWND capture via WGC; no silent desktop/screen fallback.

    start() starts native capture on its own thread. latest_frame() returns an
    immutable snapshot or raises CaptureUnavailable with a machine-readable code.
    Frames can continue while another app covers the target. A minimized,
    closed, unmappable or stale target is always rejected.
    """

    def __init__(self, hwnd: int, *, minimum_update_interval_ms: int = 80):
        self.hwnd = int(hwnd)
        self.minimum_update_interval_ms = max(80, int(minimum_update_interval_ms))
        self._lock = threading.Lock()
        self._frame: CapturedFrame | None = None
        self._sequence = 0
        self._capture = None
        self._control = None
        self._running = False
        self._error: tuple[str, str] | None = None
        self._expected_pid: int | None = None
        self._identity: dict | None = None
        self._last_copied_at = 0.0

    def _target_geometry(self) -> dict:
        geometry = _geometry(self.hwnd)
        if self._expected_pid is not None and int(geometry.get("pid", -1)) != self._expected_pid:
            raise CaptureUnavailable("window_replaced", "绑定的窗口已被替换，请重新选择窗口。")
        return geometry

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
        initial_geometry = _geometry(self.hwnd)
        self._expected_pid = int(initial_geometry["pid"])
        try:
            from native import window_identity

            self._identity = window_identity(self.hwnd)
        except Exception as exc:
            raise CaptureUnavailable("identity_unavailable", f"无法确认绑定窗口身份：{exc}") from exc
        try:
            from windows_capture import WindowsCapture
        except ImportError as exc:
            raise CaptureUnavailable("dependency_missing", "缺少窗口捕获组件，请运行安装程序。") from exc
        try:
            capture = WindowsCapture(
                window_hwnd=self.hwnd,
                cursor_capture=False,
                draw_border=None,
                # This optional native setting requires newer Windows builds.
                # Keep capture compatible with Windows 10 and throttle copying
                # in our callback instead of requiring that newer WGC interface.
                minimum_update_interval=None,
            )
        except TypeError as exc:
            raise CaptureUnavailable("capture_version", "窗口捕获组件版本不支持精确窗口绑定。") from exc

        @capture.event
        def on_frame_arrived(frame, capture_control):
            with self._lock:
                active = self._running
            if not active:
                capture_control.stop()
                return
            timestamp = time.monotonic()
            if (timestamp - self._last_copied_at) * 1000 < self.minimum_update_interval_ms:
                return
            try:
                geometry = self._target_geometry()
                # frame_buffer belongs to the native callback. Always copy it.
                pixels = np.array(frame.frame_buffer[:, :, :3], dtype=np.uint8, copy=True, order="C")
                pixels = map_capture_to_client(pixels, geometry)
                if pixels.size == 0 or max(pixels.shape[:2]) > 16384:
                    raise CaptureUnavailable("invalid_frame", "未捕获到有效窗口画面。")
                pixels.setflags(write=False)
                timestamp = time.monotonic()
                with self._lock:
                    if not self._running:
                        return
                    self._sequence += 1
                    self._frame = CapturedFrame(pixels, self._sequence, timestamp, geometry)
                    self._last_copied_at = timestamp
                    self._error = None
            except Exception as exc:
                code = exc.code if isinstance(exc, CaptureUnavailable) else "capture_error"
                message = exc.message if isinstance(exc, CaptureUnavailable) else str(exc)
                with self._lock:
                    self._frame = None
                    self._error = (code, message)

        @capture.event
        def on_closed():
            with self._lock:
                self._frame = None
                if self._running:
                    self._error = ("window_closed", "窗口捕获已结束，请重新绑定窗口。")
                self._running = False

        with self._lock:
            self._running = True
            self._frame = None
            self._error = None
            self._last_copied_at = 0.0
            self._capture = capture
        try:
            self._control = capture.start_free_threaded()
        except Exception as exc:
            with self._lock:
                self._running = False
                self._error = ("capture_start_failed", str(exc))
            raise CaptureUnavailable("capture_start_failed", f"无法开始窗口捕获：{exc}") from exc

    def latest_frame(self, max_age_ms: float = 2000) -> CapturedFrame:
        if self._identity:
            from native import validate_target

            valid, reason = validate_target(self._identity)
            if not valid:
                raise CaptureUnavailable("target_invalid", f"绑定窗口当前不可用：{reason}")
        geometry = self._target_geometry()  # Reject a newly minimized/closed/replaced target immediately.
        with self._lock:
            frame, error, running = self._frame, self._error, self._running
        if error:
            raise CaptureUnavailable(*error)
        if not running:
            raise CaptureUnavailable("capture_stopped", "窗口捕获尚未启动。")
        if frame is None:
            raise CaptureUnavailable("frame_pending", "正在等待窗口画面。")
        if (time.monotonic() - frame.timestamp) * 1000 > max_age_ms:
            raise CaptureUnavailable("frame_stale", "窗口画面已过期，识别已暂停。")
        client_rect = geometry["client_screen_rect"]
        if (client_rect[2] - client_rect[0], client_rect[3] - client_rect[1]) != (frame.image_bgr.shape[1], frame.image_bgr.shape[0]):
            raise CaptureUnavailable("window_resizing", "窗口尺寸正在变化，正在等待新画面。")
        # A move need not repaint the target. Use current geometry without
        # pretending the unchanged pixels are a more recently captured frame.
        return CapturedFrame(frame.image_bgr, frame.sequence, frame.timestamp, geometry, frame.source)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._frame = None
            control, self._control = self._control, None
        if control is not None:
            try:
                control.stop()
            except Exception:
                pass
        self._capture = None
        self._identity = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


def normalize_name(name: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", str(name))
        if not char.isspace() and char not in "\u200b\u200c\u200d\ufeff"
    ).casefold()


def _rect_pixels(rect: Sequence[float], width: int, height: int) -> tuple[int, int, int, int]:
    if len(rect) != 4:
        raise ValueError("识别区域必须包含四个归一化边界。")
    left, top, right, bottom = (float(value) for value in rect)
    if not all(np.isfinite([left, top, right, bottom])) or not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("识别区域超出窗口，请重新框选商店。")
    x0, y0 = int(np.floor(left * width)), int(np.floor(top * height))
    x1, y1 = int(np.ceil(right * width)), int(np.ceil(bottom * height))
    return max(0, x0), max(0, y0), min(width, x1), min(height, y1)


def _find_model_dir(model_dir: str | Path | None) -> Path:
    if model_dir is not None:
        return Path(model_dir)
    # PyInstaller --collect-all rapidocr extracts model resources beside the
    # package; no dependency on the developer's lab directory is retained.
    return Path(str(importlib.resources.files("rapidocr").joinpath("models")))


class VisionEngine:
    """One persistent OCR engine, serialized across callers.

    Learn whole-width name rows once, then freshly detect text within those rows.
    No previous word's horizontal box is reused to crop a new name. Changed rows
    are always recognized anew; unchanged row pixels may reuse their raw OCR.
    Periodic full-shop verification and edge/empty fallback repair layout changes.
    """

    def __init__(
        self,
        known_names: Iterable[str] = (),
        model_dir: str | Path | None = None,
        min_score: float = 0.8,
        *,
        detector_side: int = 1280,
        max_frame_age_ms: float = 5000,
        smart: bool = True,
        full_refresh_seconds: float = 10.0,
        empty_band_grace_seconds: float = .6,
        intra_op_num_threads: int = 2,
    ):
        if type(intra_op_num_threads) is not int or intra_op_num_threads not in (1, 2, 4, 8):
            raise ValueError("识别线程数必须为 1、2、4 或 8。")
        from rapidocr import RapidOCR

        self._lock = threading.Lock()
        self._closed = False
        self.min_score = float(min_score)
        self.max_frame_age_ms = float(max_frame_age_ms)
        self.smart = bool(smart)
        self.full_refresh_seconds = max(1.0, float(full_refresh_seconds))
        self.empty_band_grace_seconds = max(.05, min(2.0, float(empty_band_grace_seconds)))
        self._layout = None
        self._layout_epoch = 0
        self._last_full = None
        self._names: dict[str, str | None] = {}
        for name in known_names:
            canonical = str(name)
            key = normalize_name(canonical)
            if not key:
                continue
            if key in self._names and self._names[key] != canonical:
                self._names[key] = None  # Ambiguous normalizations cannot match.
            else:
                self._names[key] = canonical
        model_dir = _find_model_dir(model_dir)
        rec = model_dir / "PP-OCRv6_rec_small.onnx"
        det = model_dir / "PP-OCRv6_det_small.onnx"
        for path in (rec, det):
            if not path.is_file():
                raise FileNotFoundError(f"缺少本地识别模型：{path.name}")
        self.parameters = {
            "Rec.model_path": str(rec),
            # Same 48-pixel character height and aspect ratio. Dynamic-width
            # ONNX input still grows for long names; avoid padding every short
            # name to the general-document default of 320 pixels.
            "Rec.rec_img_shape": [3, 48, 96 if self.smart else 320],
            "Det.model_path": str(det),
            "Global.use_cls": False,
            "Global.text_score": 0.0,
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": intra_op_num_threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Det.limit_side_len": int(detector_side),
            "Det.limit_type": "max",
        }
        started = time.perf_counter()
        self.engine = RapidOCR(params=self.parameters)
        self.engine._load_rec_model()
        self.engine._load_det_model()
        self.initialization_ms = (time.perf_counter() - started) * 1000
        self.requested_intra_op_num_threads = intra_op_num_threads
        session_options = {}
        for component in ("text_det", "text_rec"):
            try:
                options = getattr(self.engine, component).session.session.get_session_options()
                session_options[component] = {
                    "intra_op_num_threads": options.intra_op_num_threads,
                    "inter_op_num_threads": options.inter_op_num_threads,
                    "execution_mode": str(options.execution_mode),
                }
            except Exception as exc:
                # Some alternate backends do not expose native options. Keep
                # that uncertainty visible rather than claiming params applied.
                session_options[component] = {"unavailable": str(exc)}
        self.model_info = {
            "rapidocr": importlib.metadata.version("rapidocr"),
            "onnxruntime": importlib.metadata.version("onnxruntime"),
            "recognizer": rec.name,
            "recognizer_sha256": hashlib.sha256(rec.read_bytes()).hexdigest(),
            "detector": det.name,
            "providers": self.engine.text_rec.session.session.get_providers(),
            "initialization_ms": self.initialization_ms,
            "requested_intra_op_num_threads": intra_op_num_threads,
            "requested_intra_op_threads": intra_op_num_threads,
            # Zero is ORT's automatic allocation; None is unavailable. Neither
            # value is replaced with the requested number in diagnostics.
            "effective_threads": {
                "det": session_options["text_det"].get("intra_op_num_threads"),
                "rec": session_options["text_rec"].get("intra_op_num_threads"),
            },
            "session_options": session_options,
            "parameters": self.parameters,
        }

    def reset_layout(self) -> None:
        """Invalidate spatial caches without waiting for an in-flight OCR call.

        The UI still rejects in-flight observations using its own generation.
        The epoch prevents that older call from repopulating this cleared cache.
        """
        self._layout_epoch += 1
        self._layout = None
        self._last_full = None

    def close(self) -> None:
        """Release persistent native inference sessions after the worker stops."""
        with self._lock:
            self._closed = True
            self.engine = None
            self.reset_layout()

    def _match_lines(self, raw_lines):
        return [
            {**line, "text": self._names[normalize_name(line["raw_text"])]}
            for line in raw_lines
            if line["score"] >= self.min_score and self._names.get(normalize_name(line["raw_text"]))
        ]

    @staticmethod
    def _fingerprint(image_bgr, bands):
        height, width = image_bgr.shape[:2]
        digest = hashlib.blake2b(digest_size=16)
        for band in bands:
            left, top, right, bottom = _rect_pixels(band, width, height)
            digest.update(np.ascontiguousarray(image_bgr[top:bottom, left:right]).tobytes())
        return digest.hexdigest()

    @staticmethod
    def _structure_signatures(image_bgr, bands):
        height, width = image_bgr.shape[:2]
        result = []
        for band in bands:
            left, top, right, bottom = _rect_pixels(band, width, height)
            result.append(build_name_signature(np.ascontiguousarray(image_bgr[top:bottom, left:right])))
        return result

    def _learn_bands(self, candidates, roi_norm, frame_size):
        """Discover horizontal rows from several full-catalog exact matches.

        Each band's horizontal extent is the *entire* user ROI, including empty
        slots and space needed by names longer than those visible during learning.
        """
        if len(candidates) < 2:
            return []
        width, height = frame_size
        groups = []
        for line in sorted(candidates, key=lambda row: (row["box_px"][1] + row["box_px"][3]) / 2):
            box = line["box_px"]
            center = (box[1] + box[3]) / 2
            line_height = box[3] - box[1]
            compatible = next((group for group in groups if abs(group["center"] - center) <= max(group["height"], line_height) * .8), None)
            if compatible is None:
                groups.append({"center": center, "height": line_height, "lines": [line]})
            else:
                compatible["lines"].append(line)
                compatible["center"] = float(np.median([(row["box_px"][1] + row["box_px"][3]) / 2 for row in compatible["lines"]]))
                compatible["height"] = float(np.median([row["box_px"][3] - row["box_px"][1] for row in compatible["lines"]]))
        bands = []
        for group in groups:
            # Every reliable full-scan entity title must remain represented.
            # A single equipment title can sit below the operator row, and an
            # operator with more trait lines can place its title above that row.
            line_height = group["height"]
            padding = max(8.0, line_height * .4)
            top = max(float(roi_norm[1]), (min(line["box_px"][1] for line in group["lines"]) - padding) / height)
            bottom = min(float(roi_norm[3]), (max(line["box_px"][3] for line in group["lines"]) + padding) / height)
            bands.append({"roi": [float(roi_norm[0]), top, float(roi_norm[2]), bottom],
                          "heights": [line["box_px"][3] - line["box_px"][1] for line in group["lines"]]})
        # Merge overlapping (or pixel-adjacent) full-width rows. Otherwise the
        # same visible title can be detected twice in overlapping row crops.
        merged = []
        for band in sorted(bands, key=lambda row: row["roi"][1]):
            if merged and band["roi"][1] <= merged[-1]["roi"][3] + 2 / height:
                merged[-1]["roi"][3] = max(merged[-1]["roi"][3], band["roi"][3])
                merged[-1]["heights"].extend(band["heights"])
            else:
                merged.append(band)
        return [{"roi": band["roi"], "line_height": float(np.median(band["heights"])),
                 "minimum_line_height": min(band["heights"])} for band in merged]

    def _ocr_lines(self, image_bgr, *, offset_px, frame_size, min_height_px=0.0):
        """Detect once and batch-recognize selected text lines; no name dictionary.

        Thin text clipped from neighboring description rows is filtered only in
        the learned-row fast path, using the learned name font's physical height.
        Whole-shop scans retain every detected line for conservative relearning.
        """
        from rapidocr.ch_ppocr_rec import TextRecInput
        from rapidocr.utils.process_img import get_rotate_crop_image

        started = time.perf_counter()
        detector = self.engine.text_det
        original_side = detector.limit_side_len
        if min_height_px:
            # Detection only: keep the learned name font about 28 pixels tall
            # instead of processing an unnecessarily wide high-DPI row. Text
            # recognition below still receives original-resolution crops.
            expected_height = min_height_px / .85
            detector.limit_side_len = min(original_side, max(640, round(image_bgr.shape[1] * 28 / expected_height)))
        try:
            detected = detector(image_bgr)
        finally:
            detector.limit_side_len = original_side
        det_ms = (time.perf_counter() - started) * 1000
        boxes = getattr(detected, "boxes", None)
        stats = {"det_ms": det_ms, "rec_ms": 0.0, "detected_lines": 0,
                 "recognized_lines": 0, "filtered_small_lines": 0, "edge_clipped": False}
        if boxes is None:
            return [], stats
        stats["detected_lines"] = len(boxes)
        kept, crops, edge_flags = [], [], []
        crop_height = image_bgr.shape[0]
        for polygon in boxes:
            polygon = np.asarray(polygon, dtype=np.float32)
            top, bottom = float(np.min(polygon[:, 1])), float(np.max(polygon[:, 1]))
            if bottom - top < min_height_px:
                stats["filtered_small_lines"] += 1
                continue
            touches_edge = bool(min_height_px and (top <= 2 or bottom >= crop_height - 2))
            if touches_edge:
                stats["edge_clipped"] = True
            kept.append(polygon)
            edge_flags.append(touches_edge)
            crops.append(get_rotate_crop_image(image_bgr, polygon.copy()))
        if not crops:
            return [], stats
        started = time.perf_counter()
        recognized = self.engine.text_rec(TextRecInput(img=crops))
        stats["rec_ms"] = (time.perf_counter() - started) * 1000
        texts = list(getattr(recognized, "txts", None) or [])
        scores_value = getattr(recognized, "scores", None)
        scores = [] if scores_value is None else list(scores_value)
        stats["recognized_lines"] = len(texts)
        offset_x, offset_y = offset_px
        width, height = frame_size
        raw_lines = []
        for text, score, polygon, touches_edge in zip(texts, scores, kept, edge_flags):
            box = [offset_x + float(np.min(polygon[:, 0])), offset_y + float(np.min(polygon[:, 1])),
                   offset_x + float(np.max(polygon[:, 0])), offset_y + float(np.max(polygon[:, 1]))]
            line = self._line(text, score, box, width, height)
            line["touches_band_edge"] = touches_edge
            raw_lines.append(line)
        return raw_lines, stats

    def analyze(
        self,
        image_bgr: np.ndarray,
        roi_norm: Sequence[float],
        *,
        sequence: int = 0,
        timestamp: float | None = None,
        name_regions_norm: Sequence[Sequence[float]] | None = None,
    ) -> dict:
        started = time.perf_counter()
        captured_at = time.monotonic() if timestamp is None else float(timestamp)
        result = {
            "status": "no_candidates", "sequence": int(sequence),
            "timestamp": captured_at, "observed_at": time.time(),
            "frame_size": [int(image_bgr.shape[1]), int(image_bgr.shape[0])],
            "roi_norm": [float(v) for v in roi_norm],
            "candidates": [], "raw_lines": [], "elapsed_ms": 0.0,
            "layout_source": "calibrated_name_bands" if name_regions_norm else "fresh_shop_detection",
            "stability_regions_norm": [],
            "ocr_reused": False,
            "diagnostics": {"path": "", "det_ms": 0.0, "rec_ms": 0.0, "recognized_lines": 0,
                            "detected_lines": 0, "filtered_small_lines": 0, "full_refresh_reason": None},
            "error": None,
        }
        try:
            if image_bgr.dtype != np.uint8 or image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
                raise ValueError("识别输入必须为 BGR uint8 三通道图像。")
            if timestamp is not None and (time.monotonic() - captured_at) * 1000 > self.max_frame_age_ms:
                result["status"] = "frame_stale"
                result["error"] = "画面已过期，本次不生成提醒。"
                return result
            height, width = image_bgr.shape[:2]
            x0, y0, x1, y1 = _rect_pixels(roi_norm, width, height)
            if min(x1 - x0, y1 - y0) < 8:
                raise ValueError("识别区域过小，请重新框选商店。")
            shop = np.ascontiguousarray(image_bgr[y0:y1, x0:x1])
            if float(np.std(shop)) < 1.0:
                result["status"] = "blank_frame"
                result["error"] = "识别区域没有可读画面。"
                return result
            raw_lines = []
            with self._lock:
                if self._closed:
                    raise RuntimeError("识别引擎已关闭。")
                epoch = self._layout_epoch
                context = (width, height, *(float(v) for v in roi_norm))
                diagnostics = result["diagnostics"]
                if name_regions_norm:
                    diagnostics["path"] = "explicit_name_bands"
                    result["stability_regions_norm"] = [list(region) for region in name_regions_norm]
                    for band_index, region in enumerate(name_regions_norm):
                        bx0, by0, bx1, by1 = _rect_pixels(region, width, height)
                        if bx0 < x0 or by0 < y0 or bx1 > x1 or by1 > y1:
                            raise ValueError("名称区域超出已框选商店。")
                        crop = np.ascontiguousarray(image_bgr[by0:by1, bx0:bx1])
                        rec_started = time.perf_counter()
                        prediction = self.engine(crop, use_det=False, use_cls=False, use_rec=True)
                        diagnostics["rec_ms"] += (time.perf_counter() - rec_started) * 1000
                        texts = list(getattr(prediction, "txts", None) or [])
                        scores_value = getattr(prediction, "scores", None)
                        scores = [] if scores_value is None else list(scores_value)
                        for text, score in zip(texts, scores):
                            raw_lines.append(self._line(text, score, [bx0, by0, bx1, by1], width, height, band_index))
                        diagnostics["recognized_lines"] += len(texts)
                else:
                    now = time.monotonic()
                    layout = self._layout if self.smart else None
                    full_reason = "smart_disabled" if not self.smart else "cold_start"
                    do_full = True
                    if layout and layout["context"] == context:
                        bands = [row["roi"] for row in layout["bands"]]
                        result["stability_regions_norm"] = copy.deepcopy(bands)
                        fingerprint = self._fingerprint(image_bgr, bands)
                        empty_since = layout.get("empty_since")
                        band_changed = fingerprint != layout["fingerprint"]
                        band_idle_seconds = now - layout.get("last_band_update_at", layout["verified_at"])
                        periodic_due = now - layout["verified_at"] >= self.full_refresh_seconds
                        diagnostics["periodic_pending"] = periodic_due
                        diagnostics["band_idle_ms"] = max(0.0, band_idle_seconds * 1000)
                        if (empty_since is not None and now - empty_since >= self.empty_band_grace_seconds
                                and not band_changed):
                            # Check before exact/static cache: an unchanged empty
                            # row must not conceal a reflow outside the old band.
                            full_reason = "empty_band_timeout"
                        elif periodic_due and not band_changed and band_idle_seconds >= 1.0:
                            # Periodic maintenance must never take the first turn
                            # after a shop refresh. Read changed names first, then
                            # verify the whole layout once the row is idle.
                            full_reason = "periodic_verify"
                        elif not band_changed:
                            raw_lines = copy.deepcopy(layout["raw_lines"])
                            result["ocr_reused"] = True
                            diagnostics["path"] = "unchanged_name_band"
                            do_full = False
                        else:
                            signatures = self._structure_signatures(image_bgr, bands)
                            previous_signatures = layout.get("signatures", [])
                            same_structure = empty_since is None and len(previous_signatures) == len(signatures) and all(
                                compare_name_signatures(before, after, strict=True) is True
                                for before, after in zip(previous_signatures, signatures))
                            if same_structure:
                                raw_lines = copy.deepcopy(layout["raw_lines"])
                                result["ocr_reused"] = True
                                diagnostics["path"] = "unchanged_name_structure"
                                do_full = False
                                if epoch == self._layout_epoch:
                                    # Keep the original structural reference so
                                    # small tolerated pixel changes cannot drift
                                    # step by step into a different character.
                                    self._layout = {**layout, "fingerprint": fingerprint, "last_band_update_at": time.monotonic()}
                            else:
                                diagnostics["path"] = "name_band_detection"
                        if do_full and diagnostics["path"] == "name_band_detection":
                            edge_clipped = False
                            for band in layout["bands"]:
                                bx0, by0, bx1, by1 = _rect_pixels(band["roi"], width, height)
                                lines, stages = self._ocr_lines(
                                    np.ascontiguousarray(image_bgr[by0:by1, bx0:bx1]),
                                    offset_px=(bx0, by0), frame_size=(width, height),
                                    min_height_px=band["minimum_line_height"] * .85,
                                )
                                raw_lines.extend(lines)
                                if stages.get("edge_clipped", False):
                                    edge_lines = [line for line in lines if line.get("touches_band_edge")]
                                    # A known non-name label (e.g. the funds label)
                                    # can border this band. It is already excluded
                                    # by full-catalog matching and need not cause a
                                    # full-shop scan on every frame. New edge text
                                    # and any known entity still force relearning.
                                    edge_clipped |= not edge_lines or any(
                                        line["score"] >= self.min_score
                                        and (self._names.get(normalize_name(line["raw_text"]))
                                             or (len(normalize_name(line["raw_text"])) >= 2
                                                 and not normalize_name(line["raw_text"]).isdecimal()))
                                        and
                                        normalize_name(line["raw_text"]) not in layout.get("non_name_text", set())
                                        for line in edge_lines)
                                for key in ("det_ms", "rec_ms", "recognized_lines", "detected_lines", "filtered_small_lines"):
                                    diagnostics[key] += stages.get(key, 0)
                            band_has_names = bool(self._match_lines(raw_lines))
                            if edge_clipped and band_has_names:
                                full_reason = "name_band_edge"
                            elif not band_has_names:
                                # Shop refresh/purchase animations often remove
                                # names briefly. Clear semantic evidence now,
                                # keep spatial calibration, and allow the next
                                # frame to recover without a new full-shop scan.
                                first_empty = now if empty_since is None else empty_since
                                diagnostics["empty_band_age_ms"] = max(0.0, (now - first_empty) * 1000)
                                diagnostics["empty_band_grace_ms"] = self.empty_band_grace_seconds * 1000
                                if empty_since is not None and now - empty_since >= self.empty_band_grace_seconds:
                                    # Changed pixels may be restored names. Try
                                    # the cheap name-row OCR first even on a slow
                                    # machine; only an actually still-empty row
                                    # needs full-shop layout verification now.
                                    full_reason = "empty_band_timeout"
                                else:
                                    do_full = False
                                    if epoch == self._layout_epoch:
                                        self._layout = {**layout, "fingerprint": fingerprint,
                                                        "raw_lines": [], "empty_since": first_empty,
                                                        "last_band_update_at": time.monotonic()}
                                    raw_lines = []
                            else:
                                do_full = False
                                if epoch == self._layout_epoch:
                                    self._layout = {**layout, "fingerprint": fingerprint, "raw_lines": copy.deepcopy(raw_lines),
                                                    "signatures": signatures, "empty_since": None,
                                                    "last_band_update_at": time.monotonic()}
                    elif layout:
                        full_reason = "roi_or_size_changed"
                    elif self.smart and self._last_full and self._last_full["context"] == context:
                        previous = self._last_full
                        if now - previous["verified_at"] < self.full_refresh_seconds and previous["fingerprint"] == self._fingerprint(image_bgr, [roi_norm]):
                            raw_lines = copy.deepcopy(previous["raw_lines"])
                            result["ocr_reused"] = True
                            diagnostics["path"] = "unchanged_shop_pixels"
                            result["stability_regions_norm"] = [list(roi_norm)]
                            do_full = False
                    if do_full:
                        raw_lines, stages = self._ocr_lines(shop, offset_px=(x0, y0), frame_size=(width, height))
                        for key in ("det_ms", "rec_ms", "recognized_lines", "detected_lines", "filtered_small_lines"):
                            diagnostics[key] += stages.get(key, 0)
                        diagnostics["path"] = "full_shop_detection"
                        diagnostics["full_refresh_reason"] = full_reason
                        matched = self._match_lines(raw_lines)
                        learned = self._learn_bands(matched, roi_norm, (width, height)) if self.smart else []
                        # A known shop can shrink to one or zero occupied slots.
                        # That changes observations, not its spatial name row.
                        compatible_previous = self.smart and layout and layout["context"] == context and all(
                            any(line["box_norm"][1] >= band["roi"][1] and line["box_norm"][3] <= band["roi"][3]
                                for band in layout["bands"])
                            for line in matched)
                        preserve_previous = not learned and compatible_previous
                        if preserve_previous:
                            learned = layout["bands"]
                        result["stability_regions_norm"] = [copy.deepcopy(row["roi"]) for row in learned] or [list(roi_norm)]
                        if epoch == self._layout_epoch:
                            if learned:
                                rows = [line for line in raw_lines if any(
                                    line["box_norm"][1] >= band["roi"][1] and line["box_norm"][3] <= band["roi"][3]
                                    for band in learned)]
                                signatures = (layout["signatures"] if preserve_previous and not matched else
                                              self._structure_signatures(image_bgr, [row["roi"] for row in learned]))
                                self._layout = {"context": context, "bands": learned, "verified_at": now,
                                                "last_band_update_at": now,
                                                "fingerprint": self._fingerprint(image_bgr, [row["roi"] for row in learned]),
                                                "signatures": signatures,
                                                "raw_lines": copy.deepcopy(rows) if matched else [],
                                                "empty_since": time.monotonic() if not matched else None,
                                                "non_name_text": {normalize_name(line["raw_text"]) for line in raw_lines
                                                                  if line["score"] >= .95 and len(normalize_name(line["raw_text"])) >= 2
                                                                  and not self._names.get(normalize_name(line["raw_text"]))}}
                            else:
                                self._layout = None
                            self._last_full = {"context": context, "verified_at": now,
                                               "fingerprint": self._fingerprint(image_bgr, [roi_norm]),
                                               "raw_lines": copy.deepcopy(raw_lines)}
                    result["layout_source"] = diagnostics["path"]
            result["raw_lines"] = raw_lines
            result["candidates"] = self._match_lines(raw_lines)
            # All output belongs to the supplied frame, even when a later frame
            # has arrived. The UI also rejects older sequence numbers.
            if timestamp is not None and (time.monotonic() - captured_at) * 1000 > self.max_frame_age_ms:
                result["status"] = "frame_stale"
                result["candidates"] = []
                result["error"] = "识别完成时画面已过期，本次不生成提醒。"
                self.reset_layout()
            else:
                result["status"] = "ready" if result["candidates"] else "no_candidates"
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)
            result["candidates"] = []
        finally:
            result["elapsed_ms"] = (time.perf_counter() - started) * 1000
        return result

    @staticmethod
    def _line(text, score, box, width, height, band_index=None):
        left, top, right, bottom = (float(v) for v in box)
        left, right = max(0.0, left), min(float(width), right)
        top, bottom = max(0.0, top), min(float(height), bottom)
        result = {
            "text": str(text), "raw_text": str(text), "score": float(score),
            "box_px": [left, top, right, bottom],
            "box_norm": [left / width, top / height, right / width, bottom / height],
        }
        if band_index is not None:
            result["band_index"] = int(band_index)
        return result
