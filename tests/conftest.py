"""pytest 全局配置：在导入应用前固定数据库路径与播种开关。"""
import os
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="retrocalc-pytest-")
os.environ["APP_DB_PATH"] = os.path.join(_TMP_DIR, "test.db")
os.environ["APP_SEED"] = "1"
