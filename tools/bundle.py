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

PLUGINS = [
    # core / app
    "coreelements", "coretracers", "app", "typefindfunctions", "autodetect",
    # video
    "videoconvertscale", "videorate", "videoparsersbad", "deinterlace",
    "applemedia", "libav", "osxvideosink", "opengl", "glimagesink",
    # audio
    "audioconvert", "audioresample", "audiorate", "volume", "osxaudio",
    "audioparsers", "audiomixer", "audiotestsrc", "coreaudio",
    # demux / containers
    "isomp4", "matroska", "wavparse", "id3demux", "aacparse",
    # rtp / net (AirPlay 音频走 RTP)
    "rtp", "rtpmanager", "udp", "rtsp", "soup",
    # playback
    "playback", "videotestsrc", "pbtypes",
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
    scanner = os.path.join(os.path.dirname(plugdir), "libexec", "gst-plugin-scanner")

    for sub in ("bin", "lib", "gstreamer-1.0", "libexec/gstreamer-1.0"):
        os.makedirs(os.path.join(outdir, sub), exist_ok=True)

    shutil.copy2(build_bin, os.path.join(outdir, "bin", "uxplay"))
    os.chmod(os.path.join(outdir, "bin", "uxplay"), 0o755)

    # 1) 拷贝插件。默认全量：UxPlay 的 pipeline 元素散落在很多插件里，
    #    白名单实测会漏（例如 audiofx 提供 level 元素，缺了直接 gst_parse_launch 失败）。
    all_plugins = os.environ.get("BUNDLE_ALL_PLUGINS", "1") != "0"
    if all_plugins:
        candidates = sorted(f for f in os.listdir(plugdir)
                            if f.startswith("libgst") and f.endswith((".dylib", ".so")))
    else:
        candidates = []
        for name in PLUGINS:
            for ext in (".dylib", ".so"):
                if os.path.isfile(os.path.join(plugdir, f"libgst{name}{ext}")):
                    candidates.append(f"libgst{name}{ext}")
                    break
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
    if not all_plugins:
        missing = [n for n in PLUGINS
                   if not any(os.path.isfile(os.path.join(plugdir, f"libgst{n}{e}"))
                              for e in (".dylib", ".so"))]
        print("missing plugins: " + (", ".join(missing) if missing else "(none)"))

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

    # 可执行文件 / 插件 / scanner：@loader_path/../lib/<name>
    targets = [os.path.join(outdir, "bin", "uxplay")] + copied_plugins
    if os.path.isfile(scanner_out):
        targets.append(scanner_out)
    for t in targets:
        rewrite(t, "@loader_path/../lib")

    # 4) ad-hoc 重签名（install_name_tool 会破坏原签名）
    for base in basenames:
        subprocess.run(["codesign", "--force", "--sign", "-",
                        os.path.join(libdir, base)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for t in targets:
        subprocess.run(["codesign", "--force", "--sign", "-", t],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 5) 启动脚本
    runsh = f"""#!/bin/bash
cd "$(dirname "$0")" || exit 1
export DYLD_LIBRARY_PATH="$PWD/lib"
export GST_PLUGIN_PATH_1_0="$PWD/gstreamer-1.0"
export GST_PLUGIN_SYSTEM_PATH_1_0=""
export GST_REGISTRY_1_0="$PWD/.gst-registry.bin"
[ -x "$PWD/libexec/gstreamer-1.0/gst-plugin-scanner" ] && export GST_PLUGIN_SCANNER="$PWD/libexec/gstreamer-1.0/gst-plugin-scanner"
exec "$PWD/bin/uxplay" -vsync no "$@"
"""
    rp = os.path.join(outdir, "run.sh")
    with open(rp, "w") as f:
        f.write(runsh)
    os.chmod(rp, 0o755)
    print(f"OK -> {outdir}")


if __name__ == "__main__":
    main()
