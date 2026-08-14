"""测试共用的数据文件路径与跳过装饰器。

仓库里有两份数据，用途完全不同：

- **真实下机数据**（``SAMPLE_FILE``）：用户本机的实验结果，出于隐私不进版本库。
  依赖它的用例在文件存在时照常执行全部精确断言，文件不在时整体跳过，别人
  clone 下来不会看到一屏红色的报错。
- **演示数据**（``DEMO_FILE``）：``examples/make_demo_data.py`` 生成的虚构数据，
  随仓库发布。``tests/test_demo_data.py`` 只依赖它，因此任何人和 CI 都能跑。

用法::

    from ._fixtures import SAMPLE_FILE, requires_sample_file

    @requires_sample_file
    class TestSomething(unittest.TestCase):
        ...

注意跳过条件是在**导入时**求值的：测试跑到一半再去创建或删除数据文件不会改变
本次运行的跳过结果。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # 让各测试模块 import qpcr_tool 时不依赖工作目录
    sys.path.insert(0, str(ROOT))

# 真实下机数据的确切文件名。注意 "-" 两侧是两个连续空格，是 CFX 导出时带出来的。
SAMPLE_FILE_NAME = (
    "admin_2026-06-05 20-56-53_BR010644 -  Quantification Cq Results.xlsx"
)

# 演示数据。缺失时先跑 examples/make_demo_data.py 重新生成。
DEMO_FILE = ROOT / "examples" / "demo_qPCR_data.xlsx"

SKIP_SAMPLE_REASON = "缺少真实下机数据（不随仓库发布），跳过"
SKIP_DEMO_REASON = "缺少演示数据，请先运行 examples/make_demo_data.py"


def _locate_sample_file() -> Path:
    """定位真实下机数据：先按确切文件名找，再用通配符兜底。

    文件名里那两个连续空格在复制、重命名的过程中很容易被压成一个，所以准备了
    通配符这条退路。两条路都找不到时返回确切路径，让调用方拿到一个可读的名字
    去做 ``exists()`` 判断和写进跳过说明。
    """
    exact = ROOT / SAMPLE_FILE_NAME
    if exact.is_file():
        return exact
    matches = sorted(ROOT.glob("*Quantification Cq Results.xlsx"))
    return matches[0] if matches else exact


SAMPLE_FILE = _locate_sample_file()

HAS_SAMPLE_FILE = SAMPLE_FILE.is_file()
HAS_DEMO_FILE = DEMO_FILE.is_file()

# 装饰到类或方法上都可以；装到类上时 setUpClass 不会被执行。
requires_sample_file = unittest.skipUnless(HAS_SAMPLE_FILE, SKIP_SAMPLE_REASON)
requires_demo_file = unittest.skipUnless(HAS_DEMO_FILE, SKIP_DEMO_REASON)
