"""Run the existing Cascadeur exchange under an owned Windows process tree."""
import importlib
import json
import os
from pathlib import Path
import sys
import time


def _write(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def main():
    request_path, status_path, result_path = map(Path, sys.argv[1:])
    package = Path(__file__).resolve().parent
    sys.path.insert(0, str(package.parent))
    owned = importlib.import_module(f"{package.name}.windows_owned")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    output = Path(request["output"])
    logfile = request_path.with_name("bridge.log")
    process = None
    phases = (
        ("verify.json", .94, "正在核验候选与原动作"),
        ("receive.json", .86, "已接收候选动作"),
        ("cascadeur_export.json", .76, "正在导入返回动作"),
        ("cascadeur_feet.json", .67, "已检查脚底接触"),
        ("cascadeur_apply.json", .60, "正在检查物理润色"),
        ("cascadeur_solve.json", .50, "正在检测物理求解结果"),
        ("cascadeur_ballistic.json", .44, "已处理腾空段"),
        ("cascadeur_load.json", .38, "已建立物理动画场景"),
        ("export.json", .24, "已导出交换动作"),
    )
    try:
        _write(status_path, {"progress": .10, "detail": "正在读取交换输入"})
        with logfile.open("wb") as log:
            process = owned.OwnedProcess(
                request["argv"], env={**os.environ, "PYTHONUTF8": "1"},
                cwd=request["project"], log=log,
            )
            previous = None
            while process.poll() is None:
                current = next(
                    ((factor, detail) for name, factor, detail in phases if (output / name).is_file()),
                    None,
                )
                if current is not None and current != previous:
                    _write(status_path, {"progress": current[0], "detail": current[1]})
                    previous = current
                time.sleep(.25)
        report = output / "receipt.json"
        if process.returncode or not report.is_file():
            raise RuntimeError(f"物理动画交换未完成，详情见 {logfile}")
        receipt = json.loads(report.read_text(encoding="utf-8"))
        if not receipt.get("ok"):
            raise RuntimeError(receipt.get("error", "物理动画交换未通过验证"))
        _write(result_path, {
            "ok": True, "report": str(report),
            "candidate": receipt.get("stages", {}).get("receive", {}).get("file", ""),
        })
        return 0
    except Exception as error:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(15)
        _write(result_path, {"ok": False, "error": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
