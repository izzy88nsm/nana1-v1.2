
import importlib
import sys
from pathlib import Path

def validate_imports(base_dir):
    print("📦 Validating module imports...")
    for path in Path(base_dir).rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        rel_path = path.relative_to(base_dir).with_suffix('')
        module = ".".join(rel_path.parts)
        try:
            importlib.import_module(module)
            print(f"✅ {module}")
        except Exception as e:
            print(f"❌ {module}: {e}")

if __name__ == "__main__":
    sys.path.insert(0, "src")
    validate_imports("src")
