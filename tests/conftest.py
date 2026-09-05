import sys
import types

import numpy as np

# Ensure fake mediapipe modules are available during pytest collection.
# This prevents import-time side effects from the real packages and makes tests stable in minimal environments.

def _install_fake_mediapipe_modules():
    if "mediapipe" in sys.modules:
        return
    
    # Create fake mediapipe module structure
    fake_mediapipe = types.ModuleType("mediapipe")
    fake_solutions = types.ModuleType("mediapipe.solutions")
    fake_face_detection = types.ModuleType("mediapipe.solutions.face_detection")
    fake_face_mesh = types.ModuleType("mediapipe.solutions.face_mesh")
    
    # Mock FaceDetection class
    class FakeFaceDetection:
        def __init__(self, model_selection=0, min_detection_confidence=0.5):
            pass
        
        def process(self, image):
            class FakeDetection:
                class LocationData:
                    def __init__(self):
                        self.bounding_box = types.SimpleNamespace(xmin=0.1, ymin=0.1, width=0.2, height=0.3)
                
                def __init__(self):
                    self.location_data = self.LocationData()
            
            class FakeResult:
                def __init__(self):
                    self.detections = [FakeDetection()]
            
            return FakeResult()
    
    # Mock FaceMesh class
    class FakeFaceMesh:
        def __init__(self, static_image_mode=True, max_num_faces=1, min_detection_confidence=0.5):
            pass
        
        def process(self, image):
            class FakeLandmark:
                def __init__(self):
                    self.x = 0.5
                    self.y = 0.5
                    self.z = 0.0
            
            class FaceLandmarks:
                def __init__(self):
                    self.landmark = [FakeLandmark() for _ in range(468)]
            
            class FakeResult:
                def __init__(self):
                    self.multi_face_landmarks = [FaceLandmarks()]
            
            return FakeResult()
    
    fake_face_detection.FaceDetection = FakeFaceDetection
    fake_face_mesh.FaceMesh = FakeFaceMesh
    fake_solutions.face_detection = fake_face_detection
    fake_solutions.face_mesh = fake_face_mesh
    fake_mediapipe.solutions = fake_solutions
    
    sys.modules["mediapipe"] = fake_mediapipe
    sys.modules["mediapipe.solutions"] = fake_solutions
    sys.modules["mediapipe.solutions.face_detection"] = fake_face_detection
    sys.modules["mediapipe.solutions.face_mesh"] = fake_face_mesh


_install_fake_mediapipe_modules()

# Make QMessageBox non-interactive in tests to avoid modal dialogs blocking execution.
try:
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.information = lambda *a, **k: None
    QMessageBox.warning = lambda *a, **k: None
    QMessageBox.question = lambda *a, **k: QMessageBox.Yes
except Exception:
    # If PySide6 is unavailable in the collection stage, skip patching here.
    pass
