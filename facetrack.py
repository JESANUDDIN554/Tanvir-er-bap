"""ClipMaker PC - face-tracked 9:16 reframe.

Uses YuNet (OpenCV DNN, bundled ONNX model - no download needed) to find
faces in sampled frames, then builds a smoothed crop trajectory so the
9:16 window follows the speaker instead of sitting on a dumb center crop.

Falls back to OpenCV Haar cascade (bundled with opencv-python) and finally
to plain center crop if nothing is available. All code is original,
written for this project.
"""

import os

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "face_yunet.onnx")


class FaceTracker:
    """Detects faces; returns the x-center of the most prominent face."""

    def __init__(self):
        self.backend = "none"
        self._det = None
        self._haar = None
        try:
            import cv2
            self._cv2 = cv2
            if os.path.exists(MODEL_PATH):
                self._det = cv2.FaceDetectorYN.create(
                    MODEL_PATH, "", (320, 320), 0.55, 0.3, 5000)
                self.backend = "yunet"
            else:
                cascade = os.path.join(
                    cv2.data.haarcascades,
                    "haarcascade_frontalface_default.xml")
                if os.path.exists(cascade):
                    self._haar = cv2.CascadeClassifier(cascade)
                    self.backend = "haar"
        except Exception:  # noqa: BLE001 - cv2 missing, fall back
            self._cv2 = None

    def detect_faces(self, frame_bgr):
        """Return list of (cx, area) for all detected faces."""
        if self._cv2 is None:
            return []
        h, w = frame_bgr.shape[:2]
        try:
            if self.backend == "yunet":
                self._det.setInputSize((w, h))
                _, faces = self._det.detect(frame_bgr)
                if faces is None:
                    return []
                return [(float(f[0]) + float(f[2]) / 2.0,
                         float(f[2]) * float(f[3])) for f in faces]
            if self.backend == "haar":
                gray = self._cv2.cvtColor(frame_bgr,
                                          self._cv2.COLOR_BGR2GRAY)
                found = self._haar.detectMultiScale(
                    gray, scaleFactor=1.15, minNeighbors=4,
                    minSize=(w // 8, h // 8))
                return [(float(x) + float(fw) / 2.0, float(fw * fh))
                        for x, _y, fw, fh in found]
        except Exception:  # noqa: BLE001 - never break a render
            return []
        return []

    def detect_center(self, frame_bgr, prev_cx=None):
        """Return face center x (pixels) or None.

        With prev_cx given, sticks to the face nearest the previous
        position (temporal continuity - avoids jumping between people);
        otherwise the largest face wins.
        """
        faces = self.detect_faces(frame_bgr)
        if not faces:
            return None
        if prev_cx is not None:
            return min(faces, key=lambda f: abs(f[0] - prev_cx))[0]
        return max(faces, key=lambda f: f[1])[0]


def sample_face_centers(video_path, start_s, end_s, tracker,
                        sample_every=0.75, progress_cb=None):
    """Sample face x-centers across [start_s, end_s).

    Returns list of (t_seconds, cx_or_None). Opens the video with OpenCV.
    """
    import cv2

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError("could not open video for face tracking")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0

    samples = []
    t = start_s
    n = 0
    prev_cx = None
    total = max(int((end_s - start_s) / sample_every), 1)
    while t < end_s:
        frame_no = int(t * fps)
        if total_frames and frame_no >= total_frames:
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        ok, frame = cap.read()
        if not ok or frame is None:
            samples.append((t, None))
        else:
            cx = tracker.detect_center(frame, prev_cx)
            if cx is not None and prev_cx is not None:
                # reject teleporting to a different person across the
                # frame; hold the last position instead (gap-fill
                # logic below keeps it smooth)
                if abs(cx - prev_cx) > frame.shape[1] * 0.45:
                    cx = None
            if cx is not None:
                prev_cx = cx
            samples.append((t, cx))
        n += 1
        if progress_cb and n % 4 == 0:
            progress_cb(None, None, stage_hint=(
                f"Face tracking {int(n / total * 100)}%"))
        t += sample_every
    cap.release()
    return samples


def smooth_trajectory(samples, src_w, crop_w, max_gap_s=2.5):
    """Turn raw (t, cx) samples into a smoothed (t, x_left) trajectory.

    - Forward-fills short gaps (face briefly lost), drifts to center on
      long gaps.
    - Centered moving average kills jitter.
    - Clamps the window inside the frame.
    """
    if not samples:
        return []
    center_left = (src_w - crop_w) / 2.0

    # 1. fill gaps
    filled = []
    last_cx, gap_start = None, None
    for t, cx in samples:
        if cx is not None:
            last_cx, gap_start = cx, None
            filled.append((t, cx))
        else:
            if gap_start is None:
                gap_start = t
            if last_cx is not None and (t - gap_start) <= max_gap_s:
                filled.append((t, last_cx))  # hold last position
            else:
                filled.append((t, src_w / 2.0))  # drift to center

    # 2. moving average over x (window of 5 samples)
    xs = np.array([c for _, c in filled], dtype=float)
    ts = np.array([t for t, _ in filled], dtype=float)
    k = 5
    kernel = np.ones(k) / k
    pad = k // 2
    padded = np.pad(xs, pad, mode="edge")
    smooth = np.convolve(padded, kernel, mode="valid")

    # 3. clamp to frame
    max_left = max(src_w - crop_w, 0)
    lefts = np.clip(smooth - crop_w / 2.0, 0, max_left)
    return list(zip(ts.tolist(), lefts.tolist()))


def interpolate_x(trajectory, t):
    """Linear interpolation of x_left at time t (seconds, clip-relative)."""
    if not trajectory:
        return None
    if t <= trajectory[0][0]:
        return trajectory[0][1]
    for i in range(1, len(trajectory)):
        t0, x0 = trajectory[i - 1]
        t1, x1 = trajectory[i]
        if t <= t1:
            f = (t - t0) / max(t1 - t0, 1e-6)
            return x0 + f * (x1 - x0)
    return trajectory[-1][1]
