#!/usr/bin/env python3
"""把 build/uxplay 及其 GStreamer 插件打包成可移植目录。

产物结构:
  uxplay-<flavor>/
    bin/uxplay
    lib/*.dylib
    gstreamer-1.0/*.so
    libexec/gstreamer-1.0/gst-plugin-scanner
    run.sh

所有非系统 dylib 被复制到 lib/ 并把依赖重写为 @loader_path/../lib/<name>，
lib 内部互相引用重写为 @loader_path/<name>，随后 ad-hoc 重签名。
"""
import os
import shutil
import subprocess
import sys

# 不再手写插件名单，而是列出「UxPlay 实际会创建的 GStreamer 元素」，
# 由 gst-inspect-1.0 反查每个元素属于哪个插件。手写名单必然漏（元素散落在
# 十几个插件里，例如 level 在 audiofx、videoflip 在 videofilter），漏一个就是
# 运行时 gst_parse_launch 直接失败。
REQUIRED_ELEMENTS = [
    # core / app
    "appsrc", "appsink", "queue", "capsfilter", "fakesink", "typefind",
    # video 解码与转换（-vdec 默认 decodebin，macOS 自动选 vtdec_h264 硬解）
    "decodebin", "h264parse", "h265parse", "jpegdec",
    # GStreamer 1.24+ 把 vtdec_h264/vtdec_h265 合并成单个 vtdec；两个名字都列上，
    # 不存在的那个会被自动记为 unresolved。漏了 applemedia 就会掉回软解，i5 上必卡。
    "vtdec", "vtdec_h264", "vtdec_h265", "avdec_h264",
    "videoflip", "videoconvert", "videoscale", "videorate",
    # 视频输出：glimagesink 在 macOS 上不可靠（解码正常但窗口不刷新），
    # 必须把可替换的 sink 都带上，便于 -vs 切换定位。
    "glimagesink", "caopengllayersink", "osxvideosink", "autovideosink",
    "fpsdisplaysink",
    # audio：UxPlay 的音频解码器在 audio_renderer.c 里硬编码为 avdec_aac/avdec_alac
    "avdec_aac", "avdec_alac", "audioconvert", "audioresample", "volume", "level",
    "osxaudiosink", "autoaudiosink",
]

SYSTEM_PREFIXES = ("/usr/lib/", "/System/", "/Library/Frameworks/")


def is_system_dep(dep):
    """判断是否应跳过（系统库 / 已重写过的相对路径）。"""
    if not dep:
        return True
    if dep.startswith(SYSTEM_PREFIXES):
        return True
    if dep.startswith(("@rpath/", "@executable_path/", "@loader_path/")):
        return True
    return False


def resolve_plugins(elements):
    """元素名 -> 所属插件文件名。用 gst-inspect-1.0 输出里的 Filename 反查。"""
    files, unresolved = set(), []
    for el in elements:
        try:
            out = run(["gst-inspect-1.0", el])
        except subprocess.CalledProcessError:
            unresolved.append(el)
            continue
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Filename"):
                files.add(os.path.basename(s.split(None, 1)[1].strip()))
                break
        else:
            unresolved.append(el)
    return files, unresolved


def verify_bundle(outdir, elements, must):
    """把系统插件路径清空，只用包里的插件，确认元素能解析（= 插件真的能加载）。"""
    env = dict(os.environ)
    env["GST_PLUGIN_PATH_1_0"] = os.path.join(outdir, "gstreamer-1.0")
    env["GST_PLUGIN_SYSTEM_PATH_1_0"] = ""
    env["GST_REGISTRY_1_0"] = os.path.join(outdir, ".verify-registry.bin")
    env["DYLD_LIBRARY_PATH"] = os.path.join(outdir, "lib")
    bad = []
    for el in elements:
        if el not in must:
            continue
        r = subprocess.run(["gst-inspect-1.0", el], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            bad.append(el)
    try:
        os.remove(env["GST_REGISTRY_1_0"])
    except OSError:
        pass
    return bad


INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>UxPlay</string>
    <key>CFBundleDisplayName</key><string>UxPlay</string>
    <key>CFBundleIdentifier</key><string>com.baggioyfan.uxplay-air</string>
    <key>CFBundleVersion</key><string>1.74</string>
    <key>CFBundleShortVersionString</key><string>1.74</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>UxPlay</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
    <key>NSLocalNetworkUsageDescription</key>
    <string>UxPlay 需要访问本地网络，才能被 AirPlay 发送端发现并接收画面。</string>
    <key>NSBonjourServices</key>
    <array>
        <string>_airplay._tcp</string>
        <string>_raop._tcp</string>
    </array>
</dict>
</plist>
"""

# Finder 里双击时没有终端，stdout 必须落盘，否则出问题只能靠猜。
APP_WRAPPER = """#!/bin/bash
here="$(cd "$(dirname "$0")" && pwd)"
run="$here/../Resources/runtime/run.sh"
log="$HOME/Library/Logs/UxPlay.log"
mkdir -p "$(dirname "$log")"
if [ -t 1 ]; then
    exec "$run" "$@"
else
    echo "=== $(date) launch ===" >> "$log"
    exec "$run" "$@" >> "$log" 2>&1
fi
"""


def make_app(outdir, appname="UxPlay"):
    """把可移植目录包成标准 .app。

    布局：UxPlay.app/Contents/{MacOS/UxPlay(包装脚本), Resources/runtime/...}
    保留 bin/ 与 lib/ 的相对关系，这样已重写好的 @loader_path 依赖不用再动。
    """
    root = os.path.dirname(outdir)
    app = os.path.join(root, f"{appname}.app")
    if os.path.isdir(app):
        shutil.rmtree(app)
    contents = os.path.join(app, "Contents")
    macos = os.path.join(contents, "MacOS")
    resources = os.path.join(contents, "Resources")
    os.makedirs(macos, exist_ok=True)
    os.makedirs(resources, exist_ok=True)

    shutil.move(outdir, os.path.join(resources, "runtime"))

    with open(os.path.join(contents, "Info.plist"), "w") as f:
        f.write(INFO_PLIST)
    wrapper = os.path.join(macos, appname)
    with open(wrapper, "w") as f:
        f.write(APP_WRAPPER)
    os.chmod(wrapper, 0o755)

    # ad-hoc 签名整个 bundle（内部组件已逐个签过）
    r = subprocess.run(["codesign", "--force", "--sign", "-", app],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        print("codesign warning:", r.stderr.strip()[:200])
    v = subprocess.run(["codesign", "-v", app], capture_output=True, text=True)
    print(f"app: {app} ({'signed ok' if v.returncode == 0 else 'sign check failed'})")
    return app


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw).stdout


def deps_of(path):
    out = run(["otool", "-L", path])
    lines = out.splitlines()[1:]
    result = []
    for line in lines:
        parts = line.strip().split(" ")
        if not parts or not parts[0]:
            continue
        dep = parts[0]
        if is_system_dep(dep):
            continue
        result.append(dep)
    return result


def collect(roots):
    """递归收集 roots 的非系统依赖（roots 自身不进闭包）。"""
    seen = {}          # 绝对路径 -> basename
    queue = []
    for r in roots:
        queue.extend(deps_of(r))
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        if not os.path.isfile(cur):
            print(f"  [warn] missing dep: {cur}")
            continue
        seen[cur] = os.path.basename(cur)
        for d in deps_of(cur):
            if d not in seen:
                queue.append(d)
    return seen


def main():
    build_bin = os.path.abspath(sys.argv[1])
    outdir = os.path.abspath(sys.argv[2])
    flavor = sys.argv[3] if len(sys.argv) > 3 else "air"

    plugdir = run(["pkg-config", "--variable=pluginsdir", "gstreamer-1.0"]).strip()

    # gst-plugin-scanner 的位置在不同版本/打包方式下不一致（brew 放在 prefix/libexec，
    # 官方 pkg 放在 libexec/gstreamer-1.0，也可能直接躺在 bin/）。逐个候选探测。
    scanner = None
    prefix = run(["brew", "--prefix", "gstreamer"]).strip() if shutil.which("brew") else ""
    for cand in (
        os.path.join(prefix, "libexec", "gst-plugin-scanner") if prefix else "",
        os.path.join(prefix, "libexec", "gstreamer-1.0", "gst-plugin-scanner") if prefix else "",
        os.path.join(os.path.dirname(os.path.dirname(plugdir)), "libexec", "gst-plugin-scanner"),
        os.path.join(os.path.dirname(plugdir), "libexec", "gst-plugin-scanner"),
        os.path.join(os.path.dirname(os.path.dirname(plugdir)), "bin", "gst-plugin-scanner"),
    ):
        if cand and os.path.isfile(cand):
            scanner = cand
            break
    if not scanner:
        scanner = "<not found>"
        print("scanner: searching, candidates were:", prefix or "(no brew)")

    for sub in ("bin", "lib", "gstreamer-1.0", "libexec/gstreamer-1.0"):
        os.makedirs(os.path.join(outdir, sub), exist_ok=True)

    shutil.copy2(build_bin, os.path.join(outdir, "bin", "uxplay"))
    os.chmod(os.path.join(outdir, "bin", "uxplay"), 0o755)

    # 1) 拷贝插件。默认「按元素反查」：UxPlay 实际用到的元素散落在十几个插件里，
    #    全量会带进整个 FFmpeg 家族（170MB+），手写名单又必然漏（漏 level 就崩）。
    #    BUNDLE_ALL_PLUGINS=1 可退回全量模式做对照。
    all_plugins = os.environ.get("BUNDLE_ALL_PLUGINS", "0") != "0"
    unresolved = []
    if all_plugins:
        candidates = sorted(f for f in os.listdir(plugdir)
                            if f.startswith("libgst") and f.endswith((".dylib", ".so")))
    else:
        plugin_files, unresolved = resolve_plugins(REQUIRED_ELEMENTS)
        candidates = sorted(plugin_files)
        print(f"elements resolved: {len(REQUIRED_ELEMENTS) - len(unresolved)}/{len(REQUIRED_ELEMENTS)}"
              f" -> {len(candidates)} plugins")
        if unresolved:
            print("elements NOT available on this platform: " + ", ".join(unresolved))
    copied_plugins = []
    skipped = []
    for fn in candidates:
        src = os.path.join(plugdir, fn)
        dst = os.path.join(outdir, "gstreamer-1.0", fn)
        # 有些条目是断链符号链接（如 libgstnice.dylib），isfile 为 False
        if not os.path.isfile(src):
            skipped.append(fn)
            continue
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            skipped.append(f"{fn} ({exc.strerror})")
            continue
        copied_plugins.append(dst)
    print(f"plugins copied: {len(copied_plugins)} (all={all_plugins})")
    if skipped:
        print(f"plugins skipped: {len(skipped)} -> " + ", ".join(skipped[:10]))

    # GStreamer 期望 <prefix>/libexec/gstreamer-1.0/gst-plugin-scanner
    scanner_sub = os.path.join(outdir, "libexec", "gstreamer-1.0")
    scanner_out = os.path.join(scanner_sub, "gst-plugin-scanner")
    if os.path.isfile(scanner):
        shutil.copy2(scanner, scanner_out)
        os.chmod(scanner_out, 0o755)
        print("scanner: bundled")
    else:
        print(f"scanner: NOT found at {scanner}")

    # 2) 递归收集 dylib 闭包
    roots = [os.path.join(outdir, "bin", "uxplay")] + copied_plugins
    if os.path.isfile(scanner_out):
        roots.append(scanner_out)
    closure = collect(roots)
    print(f"dylib closure: {len(closure)}")

    libdir = os.path.join(outdir, "lib")
    basenames = {}
    for src, base in closure.items():
        if base in basenames and basenames[base] != src:
            print(f"  [warn] basename clash {base}")
        try:
            shutil.copy2(src, os.path.join(libdir, base))
        except OSError as exc:
            print(f"  [warn] copy failed {base}: {exc.strerror}")
            continue
        basenames[base] = src
        os.chmod(os.path.join(libdir, base), 0o755)

    # 3) 重写依赖
    def rewrite(path, lib_prefix):
        for d in deps_of(path):
            base = os.path.basename(d)
            if not os.path.isfile(os.path.join(libdir, base)):
                continue
            subprocess.run(["install_name_tool", "-change", d,
                            f"{lib_prefix}/{base}", path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # lib 内部：@loader_path/<name>
    for base in basenames:
        p = os.path.join(libdir, base)
        subprocess.run(["install_name_tool", "-id", f"@loader_path/{base}", p],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rewrite(p, "@loader_path")

    # 可执行文件 / 插件 / scanner：指向 runtime/lib，但相对层级不同。
    #   bin/uxplay                   -> ../lib
    #   gstreamer-1.0/libgst*.dylib  -> ../lib
    #   libexec/gstreamer-1.0/...    -> ../../lib   （多一层，写错就 "Library not loaded"）
    targets = [os.path.join(outdir, "bin", "uxplay")] + copied_plugins
    for t in targets:
        rewrite(t, "@loader_path/../lib")
    if os.path.isfile(scanner_out):
        targets.append(scanner_out)
        rewrite(scanner_out, "@loader_path/../../lib")

    # 4) strip 调试符号。Homebrew 的 bottle 不做 strip，插件和 dylib 里全是符号表，
    #    体积能差近一半。strip 会破坏原有签名，所以必须排在下面的重签名之前。
    if os.environ.get("BUNDLE_STRIP", "1") != "0":
        stripped = 0
        for p in [os.path.join(libdir, b) for b in basenames] + targets:
            if subprocess.run(["strip", "-x", p], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0:
                stripped += 1
        print(f"stripped: {stripped} files")

    # 5) ad-hoc 重签名（install_name_tool / strip 都会破坏原签名）
    for base in basenames:
        subprocess.run(["codesign", "--force", "--sign", "-",
                        os.path.join(libdir, base)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for t in targets:
        subprocess.run(["codesign", "--force", "--sign", "-", t],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 6) 启动脚本
    runsh = f"""#!/bin/bash
cd "$(dirname "$0")" || exit 1
export DYLD_LIBRARY_PATH="$PWD/lib"
export GST_PLUGIN_PATH_1_0="$PWD/gstreamer-1.0"
export GST_PLUGIN_SYSTEM_PATH_1_0=""
export GST_REGISTRY_1_0="$PWD/.gst-registry.bin"
[ -x "$PWD/libexec/gstreamer-1.0/gst-plugin-scanner" ] && export GST_PLUGIN_SCANNER="$PWD/libexec/gstreamer-1.0/gst-plugin-scanner"
# 视频 sink 可用环境变量覆盖，便于在不改包的情况下逐个试：
#   UXPLAY_VS=glimagesink | caopengllayersink | osxvideosink | autovideosink
exec "$PWD/bin/uxplay" -vsync no -vs "${{UXPLAY_VS:-glimagesink}}" "$@"
"""
    rp = os.path.join(outdir, "run.sh")
    with open(rp, "w") as f:
        f.write(runsh)
    os.chmod(rp, 0o755)

    # 7) 自检：清空系统插件路径，只用包里的插件，逐个确认元素可解析。
    #    这一步是给「裁剪插件」兜底的——漏一个元素，运行时就是 gst_parse_launch 失败。
    must = set(REQUIRED_ELEMENTS) - set(unresolved)
    bad = verify_bundle(outdir, REQUIRED_ELEMENTS, must)
    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(outdir) for f in fs)
    print(f"bundle size: {total / 1048576:.1f} MB")
    if bad:
        print("!! VERIFY FAILED, elements not resolvable from bundle: " + ", ".join(bad))
        sys.exit(1)
    print(f"verify: {len(must)} elements all resolve from the bundle")
    print(f"OK -> {outdir}")

    if os.environ.get("BUNDLE_APP", "1") != "0":
        make_app(outdir, os.environ.get("BUNDLE_APP_NAME", "UxPlay"))


if __name__ == "__main__":
    main()
