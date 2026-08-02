import sys
import types

import numpy as np

# Ensure fake face_recognition modules are available during pytest collection.
# This prevents import-time side effects from the real packages (which may call quit() or
# require pkg_resources) and makes tests stable in minimal environments.

def _install_fake_face_modules():
    if "face_recognition" in sys.modules:
        return
    fake_face = types.ModuleType("face_recognition")
    fake_face.face_locations = lambda rgb, model="hog": [(10, 110, 110, 10)]
    fake_face.face_encodings = lambda rgb, locations: [np.ones(128)]
    fake_face.face_landmarks = lambda rgb, locations: [{"top_lip": [(10, 10), (20, 10)], "bottom_lip": [(10, 20), (20, 20)]}]
    fake_face.face_distance = lambda arr, emb: np.array([0.2])
    sys.modules["face_recognition"] = fake_face

    sys.modules["face_recognition_models"] = types.ModuleType("face_recognition_models")


_install_fake_face_modules()

# Make QMessageBox non-interactive in tests to avoid modal dialogs blocking execution.
try:
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.information = lambda *a, **k: None
    QMessageBox.warning = lambda *a, **k: None
    QMessageBox.question = lambda *a, **k: QMessageBox.Yes
except Exception:
    # If PySide6 is unavailable in the collection stage, skip patching here.
    pass
