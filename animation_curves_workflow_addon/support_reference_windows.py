"""Run the frozen two-segment reference transaction on a pinned Windows host."""

import copy
import hashlib
import importlib.util
import json
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE_MANIFEST = "8d1a2f4ba0c0e7330e37a82f408af79cbd28fa38cae100180492c474a38c9c43"
SOURCE = "1866d633192e47eeed545b9191a3038bcd245bbe8ae990cbe85c6ab2855fe39a"
PLATFORM = {"system": "Windows", "machine": "AMD64"}
from .host_compat import is_supported_host

APPLIED = "SAVED_PRIVATE_ROLLBACK_VERIFIED_PENDING_COLD"
COLD_PASS = "V2_LEFT_SUPPORT_COLD_PASS_NOT_VISUAL_OR_CONTACT_LOCK"


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def write(path, payload):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, allow_nan=False)


def assert_platform():
    need(platform.system() == PLATFORM["system"] and platform.machine().upper() == PLATFORM["machine"],
         "Unsupported platform for Windows x64 reference")


EXTENDED_PREFIX = "\\\\?\\"

def normal_path(path):
    path = Path(str(path).removeprefix(EXTENDED_PREFIX))
    need(path.is_absolute() and len(path.drive) == 2 and path.drive[0].isalpha()
         and path.drive[1] == ":" and ".." not in path.parts, "Use a direct local Windows volume")
    return path


def filesystem_path(path):
    return Path(EXTENDED_PREFIX + str(normal_path(path)))


def resolve_resource(root, relative):
    from .support_bundle_base import resolve
    from .windows_owned import safe

    return safe(resolve(filesystem_path(root), relative))


def runtime_files(runtimes):
    from .windows_owned import safe

    assert_platform()
    need(set(runtimes) == {"blender", "host_python"}, "Explicit runtime pair required")
    blender = safe(normal_path(runtimes["blender"]))
    host = safe(normal_path(runtimes["host_python"]))
    observed = subprocess.run([str(blender), "--version"], capture_output=True, text=True,
                              timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    version = re.search(r"^Blender (\d+)\.(\d+)\.(\d+)", observed.stdout, re.MULTILINE)
    build_hash = re.search(r"^\s*build hash:\s*([0-9a-f]+)", observed.stdout, re.MULTILINE)
    need(not observed.returncode and version and build_hash, "Cannot identify the selected Blender")
    number = tuple(map(int, version.groups()))
    need(is_supported_host(number), "Windows reference requires Blender 5.3.0 or later")
    folder = ".".join(version.groups()[:2])
    expected_host = safe(blender.parent / folder / "python/bin/python.exe")
    need(host.is_file() and host.samefile(expected_host), "Use the interpreter bundled with the selected Blender")
    copies = sorted(host.parent.glob("python3[0-9][0-9].dll"))
    need(len(copies) == 1, "Expected one bundled Python runtime DLL")
    dlls = {str(safe(path)): sha(path)
            for path in (copies[0], blender.with_name(copies[0].name))}
    need(len(set(dlls.values())) == 1, "Bundled Python runtime libraries disagree")
    build = {"version": number, "build": build_hash[1], "blender": sha(blender),
             "host_python": sha(host), "python_dll": next(iter(dlls.values()))}
    return build, dlls


def compile_windows(root, digest, runtimes, source, output):
    from .support_bundle_base import compile_manifest
    from .windows_owned import safe

    root = safe(root)
    source = safe(source)
    output = safe(output)
    manifest_path = safe(root / "bundle.json")
    need(digest == BASE_MANIFEST and sha(manifest_path) == digest, "Frozen reference bundle changed")
    need(source.suffix.lower() == ".blend" and source.is_file() and sha(source) == SOURCE,
         "Unsupported source for Windows reference")
    manifest = read(manifest_path)
    need(manifest["platform"] == {"system": "Linux", "machine": "x86_64"}
         and manifest["runtime_policy"] == "EXACT_HASH_EXPLICIT_ONLY", "Unexpected reference bundle recipe")
    build, dlls = runtime_files(runtimes)
    manifest = copy.deepcopy(manifest)
    for row in manifest["resources"].values():
        if "external" in row:
            row["sha256"] = build[row["external"]]
        else:
            resolve_resource(root, row["relative"])
    plan = compile_manifest(root, manifest, runtimes, source, output, digest, resolve_resource)["plan"]
    plan["backend"]["binary_sha256"] = build["blender"]
    runner = safe(Path(__file__))
    ownership = safe(runner.with_name("windows_owned.py"))
    plan["windows_reference"] = {
        "platform": PLATFORM,
        "version": list(build["version"]),
        "build": build["build"],
        "runtime_ownership": str(ownership),
        "runner": str(runner),
        "host_python": str(safe(runtimes["host_python"])),
    }
    plan["pins"].update({**dlls, str(runner): sha(runner), str(ownership): sha(ownership), str(manifest_path): digest})
    return manifest, plan


def adapt_snapshot_reader(plan, session, transforms):
    host = plan["windows_reference"]
    if not is_supported_host(host["version"]):
        return
    # The frozen serializer enumerates scalar RNA. 5.3 adds a derived, read-only
    # hash for linked packed data. The reference contains only empty local hashes.
    source = Path(plan["backend"]["snapshot_reader"]).read_text(encoding="utf-8")
    old_runtime = "tuple(bpy.app.version)==(5,2,1)"
    old_validate = "  validate_rig(bpy,self.rig,self.manifest)"
    need(source.count(old_runtime) == source.count(old_validate) == 1, "Frozen snapshot reader layout changed")
    runtime = f"tuple(bpy.app.version)=={tuple(host["version"])} and bpy.app.build_hash.decode()=={host["build"]!r}"
    canonicalize = """  scalar=self.portable.scalar
  def canonical_scalar(item):
   row=scalar(item)
   if 'deep_hash' in row:
    prop=item.bl_rna.properties['deep_hash']
    need(isinstance(item,bpy.types.ID) and item.library is None and prop.is_readonly and prop.type=='STRING' and row['deep_hash']==[0]*16,'unexpected 5.3 packed linked data hash')
    del row['deep_hash']
   return row
  self.portable.scalar=canonical_scalar
"""
    reader = session / "snapshot-reader-5.3.py"
    with reader.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(source.replace(old_runtime, runtime).replace(old_validate, canonicalize + old_validate))
    digest = sha(reader)
    plan["backend"]["snapshot_reader"] = str(reader)
    plan["pins"][str(reader)] = digest
    transforms.append({"role": "snapshot-reader-5.3", "path": str(reader), "sha256": digest})

def prepare(root, digest, runtimes, source, output, session):
    from .support_portable import prepare_compiled

    manifest, plan = compile_windows(root, digest, runtimes, source, output)
    return prepare_compiled(root, digest, runtimes, session, manifest, plan, platform_info=PLATFORM,
                            resource_resolver=resolve_resource, transform_plan=adapt_snapshot_reader)


def validate_prepared(receipt):
    from .windows_owned import safe

    assert_platform()
    need(receipt["platform"] == PLATFORM and receipt["bundle_digest"] == BASE_MANIFEST
         and sha(safe(Path(receipt["bundle_root"]) / "bundle.json")) == BASE_MANIFEST,
         "Frozen reference bundle changed")
    plan_file = safe(receipt["plan"])
    need(sha(plan_file) == receipt["plan_sha256"], "Prepared plan changed")
    plan = read(plan_file)
    build, _ = runtime_files(receipt["runtimes"])
    version = plan["windows_reference"]
    need(version["platform"] == PLATFORM and version["version"] == list(build["version"])
         and version["build"] == build["build"] and receipt["runner"] == version["runner"]
         and Path(version["runner"]) == Path(__file__)
         and plan["binary"] == receipt["runtimes"]["blender"]
         and version["host_python"] == receipt["runtimes"]["host_python"]
         and plan["backend"]["binary_sha256"] == build["blender"],
         "Prepared Windows runtime identity changed")
    need(not safe(plan["output"]).exists(), "Output consumed")
    need(sha(safe(plan["source"])) == plan["source_sha256"] == SOURCE, "Source changed")
    need(all(sha(safe(path)) == digest for path, digest in plan["pins"].items()), "Frozen reference pin changed")
    return plan


def launch(receipt, log, approved_digest):
    from .windows_owned import OwnedProcess, private_directory, private_env, safe

    need(receipt["plan_sha256"] == approved_digest, "Prepared plan approval changed")
    plan = validate_prepared(receipt)
    session = safe(Path(plan["output"]).parent)
    host = plan["windows_reference"]["host_python"]
    runner = plan["windows_reference"]["runner"]
    profile = private_directory(session / "host-profile")
    cancel_marker = session / "cancel-requested"
    command = [host, "-I", "-S", "-B", runner, receipt["plan"], receipt["plan_sha256"]]
    process = OwnedProcess(command, env=private_env(profile, Path(host)), cwd=session, log=log)
    return process, cancel_marker


def request_cancel(marker):
    try:
        with marker.open("xb"):
            pass
    except FileExistsError:
        pass


def custody_module(plan):
    location = Path(plan["windows_reference"]["runtime_ownership"])
    need(location == Path(__file__).with_name("windows_owned.py")
         and sha(location) == plan["pins"][str(location)], "Windows process ownership module changed")
    spec = importlib.util.spec_from_file_location("windows_owned", location)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def copy_exact(safe, source, target, digest):
    source, target = safe(source), safe(target)
    need(sha(source) == digest, "Copy source changed")
    with source.open("rb") as stream, target.open("xb") as copy_stream:
        shutil.copyfileobj(stream, copy_stream)
    need(sha(target) == digest and target.stat().st_nlink == 1, "Private copy mismatch")


def wait_child(child, stage, stop_requested, seconds):
    deadline = time.monotonic() + seconds
    try:
        while not stop_requested() and time.monotonic() < deadline:
            try:
                stage["exit_code"] = child.wait(timeout=0.2)
                stage["waited"] = True
                return
            except subprocess.TimeoutExpired:
                continue
        if not stop_requested():
            stage["errors"].append("Blender stage timed out")
    finally:
        if not stage["waited"]:
            if child.poll() is None:
                child.kill()
            stage["exit_code"] = child.wait()
            stage["waited"] = True


def run(plan_path, digest):
    assert_platform()
    plan_path = Path(plan_path)
    need(sha(plan_path) == digest, "Prepared plan changed")
    plan = read(plan_path)
    need(plan["status"] == "HOST_PREPARED_APPLY_COLD_NOT_ADMITTED"
         and plan["source_sha256"] == SOURCE and Path(plan["windows_reference"]["runner"]) == Path(__file__),
         "Unrecognized frozen reference plan")
    win = custody_module(plan)
    safe = win.safe
    need(sha(safe(plan_path)) == digest, "Prepared plan changed")
    pins = {**plan["pins"], str(plan_path): digest}
    output = win.private_directory(safe(plan["output"]))
    marker = safe(output.parent / "cancel-requested")
    summary = {"errors": [], "signals": [], "stages": [], "platform": PLATFORM}
    started = time.monotonic()

    def check_pins(expected):
        return {path: Path(path).is_file() and sha(safe(path)) == wanted for path, wanted in expected.items()}

    def stopped():
        if marker.exists():
            if not summary["signals"]:
                summary["signals"].append("WINDOWS_CANCEL_REQUESTED")
            return True
        return False

    def run_stage(stage, private_file, input_digest, expected=None):
        directory = win.private_directory(output / stage)
        profile = win.private_directory(directory / "profile")
        env = win.private_env(profile, Path(plan["binary"]))
        stage_pins = pins if expected is None else {**pins, **expected}
        config = {**plan["backend"], "times": plan["times"], "pins": stage_pins,
                  "stage": stage, "output": str(filesystem_path(directory)), "private_input": str(private_file),
                  "candidate_sha256": input_digest}
        if stage == "cold":
            config["expected_snapshot"] = str(output / "apply" / "after" / "SNAPSHOT.json")
            config["expected_evaluation"] = str(output / "apply" / "APPLY-EVALUATION.json")
        config_path = directory / "CONFIG.json"
        write(config_path, config)
        config_digest = sha(config_path)
        argv = [plan["binary"], "--background", "--factory-startup", "--disable-autoexec", "--offline-mode",
                "--threads", "2", str(private_file), "--python-exit-code", "1", "--python", plan["entry"], "--",
                str(config_path), config_digest]
        stage_receipt = {"stage": stage, "command": argv, "spawned": False, "waited": False, "exit_code": None,
                         "errors": [], "signals": summary["signals"], "platform": PLATFORM}
        stage_start = time.monotonic()
        try:
            need(not stopped() and all(check_pins(stage_pins).values()), "Stage stopped or input pins changed")
            with (directory / "native.log").open("xb") as log:
                child = subprocess.Popen(argv, env=env, cwd=directory, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                stage_receipt.update(spawned=True, pid=child.pid)
                wait_child(child, stage_receipt, stopped, plan["timeout_per_stage_seconds"])
            need(stage_receipt["exit_code"] == 0 and not stopped() and not stage_receipt["errors"],
                 "Blender stage failed or was stopped")
            receipt = read(directory / "ENTRY-RESULT.json")
            need(receipt["function_returned"] and not receipt["errors"]
                 and receipt["config_sha256"] == config_digest == sha(config_path), "Invalid native entry receipt")
            return receipt
        except BaseException as exc:
            stage_receipt["errors"].append(repr(exc))
            raise
        finally:
            stage_receipt["elapsed_seconds"] = time.monotonic() - stage_start
            try:
                stage_receipt["postchecks"] = {
                    "pins": all(check_pins(stage_pins).values()),
                    "private": sha(safe(private_file)) == input_digest,
                }
            except OSError as exc:
                stage_receipt["errors"].append(repr(exc))
                stage_receipt["postchecks"] = {"pins": False, "private": False}
            exit_path = directory / "EXIT.json"
            write(exit_path, stage_receipt)
            summary["stages"].append({k: stage_receipt[k] for k in ("stage", "spawned", "waited", "exit_code")})
            summary["stages"][-1]["exit_path"] = str(exit_path)
            need(all(stage_receipt["postchecks"].values()), "Blender stage changed protected inputs")

    try:
        need(not stopped() and all(check_pins(pins).values()), "Reference stopped or input pins changed")
        private_source = output / "source.private.blend"
        copy_exact(safe, plan["source"], private_source, SOURCE)
        applied = run_stage("apply", private_source, SOURCE)
        need(applied["status"] == APPLIED, "Reference apply status mismatch")
        apply_result = read(output / "apply" / "APPLY-RESULT.json")
        candidate = safe(normal_path(apply_result["saved_candidate"]["path"]))
        candidate_digest = apply_result["saved_candidate"]["sha256"]
        need(apply_result["rollback_verified"] and not apply_result["errors"]
             and candidate == output / "apply" / "candidate.blend" and sha(candidate) == candidate_digest,
             "Reference candidate or rollback mismatch")
        need(not stopped(), "Reference stopped before cold reopen")
        apply_stage = filesystem_path(output / "apply")
        expected = {str(safe(path)): sha(path) for path in apply_stage.rglob("*")
                    if "profile" not in path.relative_to(apply_stage).parts and path.is_file()}
        private_candidate = output / "cold.private.blend"
        copy_exact(safe, candidate, private_candidate, candidate_digest)
        cold = run_stage("cold", private_candidate, candidate_digest, expected)
        cold_result = read(output / "cold" / "COLD-RESULT.json")
        need(cold["status"] == cold_result["status"] == COLD_PASS and cold_result["rows"] == len(plan["times"])
             and 0 <= cold_result["maximum_numeric_delta"] < 1e-5
             and not cold_result["saved"] and not stopped(), "Reference cold result mismatch")
        result = {"status": COLD_PASS, "candidate": str(candidate), "candidate_sha256": candidate_digest,
                  "actual_evaluation": str(output / "cold" / "COLD-EVALUATION.json"),
                  "no_original_save": True, "rendered": False, "art_adopted": False}
    except BaseException as exc:
        summary["errors"].append(repr(exc))
        raise
    finally:
        try:
            summary["pins_unchanged"] = check_pins(pins)
        finally:
            summary["elapsed_seconds"] = time.monotonic() - started
            write(output / "EXIT.json", summary)
    need(not summary["errors"] and not stopped() and all(summary["pins_unchanged"].values()),
         "Reference inputs changed or cancellation arrived")
    write(output / "RESULT.json", result)
    return result


if __name__ == "__main__":
    need(len(sys.argv) == 3, "Use the prepared plan and its SHA-256")
    print(json.dumps(run(sys.argv[1], sys.argv[2]), indent=2))
