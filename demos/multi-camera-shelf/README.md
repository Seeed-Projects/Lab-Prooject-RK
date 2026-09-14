# 项目说明与 RK3588 部署指南

文档版本：1.0  
整理日期：2026-08-07  
项目目录：`RK3588-retail-shelf-monitor`  
项目版本：`0.2.0-rknn-fp16-pre-board-validation`

## 一、项目概述

`RK3588-retail-shelf-monitor` 是货架商品监控项目的独立 RK3588 版本，包含模型、推理代码、固定货架映射、演示逻辑、输入视频和参考输出。

代码、模型、货架位置、商品名称、拿货事件、输入视频和参考输出都使用相对路径关联。部署和运行时应保持目录结构完整，否则可能造成画面或业务结果与 PC 版本不一致。

当前项目已经包含：

- RK3588 推理应用代码；
- 与 PC 版相同的货架业务和绘图代码；
- 两个 YOLO11n 的 PT 源模型；
- 两个已导出的 ONNX 模型；
- 两个面向 RK3588 的 FP16 RKNN 模型；
- ONNX/RKNN 转换脚本和转换日志；
- 测试输入视频；
- PC 端标准参考输出视频；
- RKNNLite 板端安装方式和版本要求；
- 一份 AArch64 `librknnrt.so`；
- 系统探测、安装、运行和完整性检查工具。

两个 RKNN 已在 `rknn-toolkit2 2.3.2` 的 PC 模拟器中完成张量结构验证。

## 二、项目目录与文件说明

部署时使用完整目录：

```text
RK3588-retail-shelf-monitor/
```

各部分用途如下。

| 路径 | 运行所需 | 用途 |
|---|---:|---|
| `README.md` | 是 | 项目、部署和验收总说明 |
| `VERSION` | 是 | 标识当前项目版本 |
| `app/` | 是 | RK3588 视频推理入口、切片检测和类型映射 |
| `runtime/` | 是 | RKNNLite 封装、预处理和 YOLO11 后处理 |
| `shelf_monitor/` | 是 | ROI、库存、时序和视频绘制逻辑 |
| `configs/` | 是 | 模型路径、阈值、货架区域、商品名和事件时间线 |
| `models/source_pt/` | 是 | 两个训练后的 YOLO11n 源模型 |
| `models/onnx/` | 是 | 两个可复用的 ONNX 中间模型 |
| `models/rknn/` | 是 | RK3588 实际加载的两个 FP16 RKNN 模型 |
| `conversion/` | 是 | PT→ONNX、ONNX→RKNN 脚本和转换日志 |
| `requirements-conversion.txt` | 是 | 转换电脑的依赖说明 |
| `requirements-rk3588.txt` | 是 | RK3588 板端 Python 依赖 |
| `rknn-toolkit-lite2-packages/` | 是 | 板端离线安装 RKNNLite 2.3.2 |
| `lib/librknnrt.so` | 是 | 已找到的 AArch64 Runtime 2.3.0，供版本核对或临时测试 |
| `scripts/` | 是 | 安装、探测、转换和运行入口 |
| `tools/` | 是 | 项目完整性检查和 RK3588 环境采集工具 |
| `input/demo.mp4` | 是 | 标准测试输入 |
| `reference/restock_demo_pc.mp4` | 是 | PC 端标准输出，板端验收基准 |
| `outputs/` | 是 | 板端生成结果目录 |
| `diagnostics/` | 是 | 板端探测报告目录 |
| `tests/` 和 `conftest.py` | 建议 | 不依赖 NPU 的基础测试 |

RK3588 独立目录中的 PT 模型、输入视频、参考视频、业务模块和关键配置已经与原 PC 工程逐文件核对一致。板端运行不依赖原 PC 工程中的历史训练目录。

## 三、本机临时文件

以下内容不属于项目运行所需文件：

- `.venv/`、`.venv-convert/`；
- `__pycache__/`、`*.pyc`；
- `.pytest_cache/`；
- Ultralytics 用户配置缓存；
- Windows Python 环境；
- Windows DLL；
- 历史训练目录 `runs/`；
- 与当前版本无关的旧模型；
- 未验收的临时输出视频；
- 本机或其他板卡生成的旧 `diagnostics/*.json`。

原始训练数据只在重新训练或进行 INT8 校准时使用，当前 FP16 推理不依赖训练数据集。

## 四、当前模型清单

两个模型的基础网络都是 YOLO11n。

| 用途 | PT | ONNX | RKNN | 输入 | 输出 |
|---|---|---|---|---:|---|
| 货架商品检测 | `models/source_pt/shelf_product.pt` | `models/onnx/shelf_product.onnx` | `models/rknn/shelf_product.rknn` | 640×640 | `[1,5,8400]` |
| 手持商品检测 | `models/source_pt/held_product_v4.pt` | `models/onnx/held_product_v4.onnx` | `models/rknn/held_product_v4.rknn` | 320×320 | `[1,5,2100]` |

当前关键文件 SHA256：

| 文件 | SHA256 |
|---|---|
| `models/source_pt/shelf_product.pt` | `537d8e445f024c4dd84a6c67fb50485cb64d80bfca5bd5c897182893a29e4a8a` |
| `models/source_pt/held_product_v4.pt` | `0819a577ffdcd0e29e1b04b8350470f6c37851eb1bf4ac6ab4b99d1b18a00fe3` |
| `models/onnx/shelf_product.onnx` | `08114b73324c3a78bbcdb6476d750a74740cd710532230bb26555f68c8abc269` |
| `models/onnx/held_product_v4.onnx` | `52bebfb841b074a52b06cae0051be7889aa174311643fdaf5d70e1f7642857d6` |
| `models/rknn/shelf_product.rknn` | `87b8c1d6dc7a7dff9d5732081bffbd040cf4ffd27db18ab77e56cd37d19a2ef9` |
| `models/rknn/held_product_v4.rknn` | `dc2925c4fec64b133e1aebda91256f6fafd6a253201066faee15c30a7bef74fe` |
| `input/demo.mp4` | `e8e7c64cc9888a1c74569aee02d94e7922c76aece392f948b22855363ce3800a` |
| `reference/restock_demo_pc.mp4` | `2f4df5d463644351dd92b1f5f74c002538ac094ce1294d3c2ca3e76d7c5f41ba` |
| 板端 Lite2 wheel | `bda74f1179e15fccb8726054a24898982522784b65bb340b20146955d254e800` |
| `lib/librknnrt.so` | `cf6ea624489225ddc9d334b370c64083b0fd9d6a5c9166eed47d999cc85a806b` |

转换机独立附件中的 Toolkit2 wheel SHA256：

```text
6cb783ddf293ac509f39bf9127acf6a5492bbb67e4b4b4ac33a7c6d2cefb4f3c
```

当前 RKNN 转换参数：

```text
目标平台：rk3588
Toolkit2：2.3.2
量化：关闭
精度：FP16
输入布局：NHWC
输入数据：uint8 RGB
归一化：mean=[0,0,0], std=[255,255,255]
```

选择 FP16 是为了先尽量保持 PT/ONNX 效果。

## 五、模型转换环境和可复用文件

### 5.1 这次实际使用的转换环境

```text
系统：WSL2 Ubuntu 22.04
架构：x86_64
Python：3.10.12
rknn-toolkit2：2.3.2
NumPy：1.26.4
ONNX：1.14.1
```


### 5.2 已保存的可复用转换文件

```text
conversion/export_onnx.py
conversion/convert_onnx_to_rknn.py
scripts/export_all_onnx.sh
scripts/convert_all_rknn.sh
conversion/logs/
models/onnx/
models/rknn/
```

重新转换全部模型：

```bash
./scripts/export_all_onnx.sh
./scripts/convert_all_rknn.sh
```

`convert_all_rknn.sh` 会在构建后立即执行一次 PC 模拟器零输入推理，并保存日志。只有输出形状分别为 `[1,5,8400]` 和 `[1,5,2100]` 才符合当前后处理约定。

## 六、当前版本配套情况

| 组件 | 当前版本 | 结论 |
|---|---|---|
| RKNN-Toolkit2 | 2.3.2 | 已用于生成两个 RKNN |
| RKNN-Toolkit-Lite2 wheel | 2.3.2、CPython 3.11、AArch64 | 可用于 Python 3.11 的 RK3588 |
| `lib/librknnrt.so` | 2.3.0、AArch64 | 架构正确，但版本没有与 2.3.2 完全对齐 |
| RKNPU 内核驱动 | 未知 | 必须在板端确认 |
| RK3588 系统和 Python | 未知 | 必须在板端确认 |

推荐最终统一为：

```text
rknn-toolkit2       2.3.2
rknn-toolkit-lite2  2.3.2
librknnrt.so         2.3.2
```

现有 `lib/librknnrt.so` 的静态检查结果：

```text
ELF64
AArch64
librknnrt version: 2.3.0 (c949ad889d@2024-11-07T11:35:33)
```

它可以作为备用或临时测试文件，但不要覆盖板卡系统中已经存在的 2.3.2 或厂家提供的兼容版本。由于两个 RKNN 是用 Toolkit2 2.3.2 生成的，正式部署优先取得 `librknnrt.so 2.3.2`。

## 七、当前演示逻辑和前后端一致性

当前程序是离线视频推理程序，不是 Web 服务，也没有 HTTP/WebSocket 接口。它读取一个视频，处理后输出一个 MP4。

输入：

```text
input/demo.mp4
```

输出：

```text
outputs/restock_demo_rk3588.mp4
```

PC 标准参考：

```text
reference/restock_demo_pc.mp4
```

需要保持一致的文件：

- `app/infer_video_rknn.py`；
- `configs/runtime.json`；
- `configs/shelf_regions_freshco.json`；
- `configs/shelf_regions_freshco_coarse.json`；
- `configs/shelf_type_groups_freshco.json`；
- `configs/shelf_type_names_freshco.json`；
- `configs/scripted_demo_events.json`；
- `shelf_monitor/`；
- `reference/restock_demo_pc.mp4`。

商品显示名称固定为：

```text
RICE, SILK, ALMOND, EARTH, NATURA,
ZEVIA, LAKEWOOD, SANTA, BERRY, KIJU
```

当前商品名称并不是模型进行 10 类分类得到的，而是根据商品框中心所在的固定货架区域映射。固定机位不变时这种方式更稳定；更换摄像头位置、分辨率、裁剪方式或货架布局后必须检查 ROI。

当前库存演示事件也是固定配置：

```text
12.8 秒：减少 3 件
19.2 秒：减少 2 件
库存：24 → 21 → 19
```

这些事件来自 `configs/scripted_demo_events.json`，不是完全由模型实时计算。产品展示时必须了解这个边界。

如后续接入 Web 前端、数据库或消息系统，需要再定义并实现正式 API，至少包括时间戳、库存数、商品类型、拿货数量、置信度和事件类型。当前项目没有网络接口，接口字段和通信方式尚未定义。

## 八、RK3588 从空环境部署

### 8.1 安装厂家系统和 NPU 驱动

如果开发板是空环境，优先烧录开发板厂家针对具体型号提供的 Ubuntu/Debian 镜像。镜像应包含 RK3588 的 RKNPU 内核驱动。

系统启动后先执行：

```bash
uname -m
cat /etc/os-release
python3 --version || true
dmesg | grep -i rknpu
ls -l /dev/rknpu* 2>/dev/null || true
```

要求：

- `uname -m` 应输出 `aarch64`；
- 内核日志应能看到 RKNPU 初始化信息；
- 如果完全没有 RKNPU 信息，应处理系统镜像或驱动，pip 安装不能解决内核驱动问题。

### 8.2 安装基础软件

Ubuntu/Debian 示例：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg libglib2.0-0
```

当前准备的 Lite2 wheel 名称包含 `cp311`，只适配 Python 3.11。如果板端默认是 Python 3.10 或 3.12，应取得对应的 `cp310` 或 `cp312` AArch64 wheel，或者明确安装并使用 Python 3.11。不要强行把 `cp311` wheel 安装到其他 Python 版本。

### 8.3 检查 RKNN Runtime

```bash
ldconfig -p | grep -i rknn || true
find /usr /lib /opt -name 'librknnrt.so' 2>/dev/null
```

如果厂家镜像已有运行库，先确认其版本，不要直接覆盖。正式部署优先使用 2.3.2。

若只是临时加载项目目录里的 2.3.0：

```bash
export LD_LIBRARY_PATH="$PWD/lib:${LD_LIBRARY_PATH:-}"
```

### 8.4 安装板端 Python 环境

```bash
cd RK3588-retail-shelf-monitor
chmod +x scripts/*.sh

./scripts/install_runtime.sh \
  rknn-toolkit-lite2-packages/rknn_toolkit_lite2-2.3.2-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
```

验证：

```bash
.venv/bin/python -c "import numpy, cv2; print(numpy.__version__, cv2.__version__)"
.venv/bin/python -c "from rknnlite.api import RKNNLite; print('RKNNLite import OK')"
```

如果开发板不能联网，仅有 Lite2 wheel 还不够，还需要提前准备 AArch64 版本的 NumPy、OpenCV、psutil、ruamel.yaml 和 PyYAML，或者通过厂家系统仓库安装。

### 8.5 生成环境报告

```bash
./scripts/probe.sh
```

报告位置：

```text
diagnostics/rk3588_probe.json
```

执行后应保存该文件和完整终端日志，便于排查板端环境问题。

## 九、运行项目

先做完整性检查：

```bash
python3 tools/check_package.py --require-rknn
```

随后运行：

```bash
./scripts/run_demo.sh
```

或者：

```bash
.venv/bin/python app/infer_video_rknn.py --config configs/runtime.json
```

首次推理会打印两个 RKNN 的实际输出张量数量和形状。必须保存这段日志。如果板端输出形状不是 `[1,5,8400]` 和 `[1,5,2100]`，应停止验收并调整导出或后处理。

## 十、板端验收标准

### 环境

- [ ] 板卡是 RK3588/RK3588S 系列；
- [ ] 系统架构是 AArch64；
- [ ] RKNPU 内核驱动已经加载；
- [ ] Python 与 Lite2 wheel 标签匹配；
- [ ] `librknnrt.so` 可被加载；
- [ ] Toolkit2、Lite2、Runtime 和驱动版本已经记录；
- [ ] OpenCV 和 FFmpeg 能读取、写入 MP4；
- [ ] 已生成 `diagnostics/rk3588_probe.json`。

### 模型

- [ ] 两个 RKNN 均可 `load_rknn`；
- [ ] 两个 RKNN 均可 `init_runtime`；
- [ ] 货架模型输出形状正确；
- [ ] 手持模型输出形状正确；
- [ ] 两个模型没有互换文件名或输入尺寸；
- [ ] 记录实际 NPU 推理 FPS 和总耗时。

### 画面

- [ ] 能完整处理 `input/demo.mp4`；
- [ ] 成功生成 `outputs/restock_demo_rk3588.mp4`；
- [ ] 货架商品显示绿色框和商品名称；
- [ ] 手持商品显示黄色框；
- [ ] 同时拿多件商品时能显示多个黄色框；
- [ ] 12.8 秒附近显示减少 3 件；
- [ ] 19.2 秒附近显示减少 2 件；
- [ ] 库存变化为 `24 → 21 → 19`；
- [ ] 画面不显示 `SCRIPT DEMO`；
- [ ] 画面不显示旧文本 `ITEM PICKED UP: Bottom Shelf 1`；
- [ ] 与 `reference/restock_demo_pc.mp4` 分段对比，无明显框位置或名称差异。

## 十一、当前已知风险

1. Lite2 wheel 是 2.3.2，但随包 `librknnrt.so` 是 2.3.0，正式板端应尽量对齐为 2.3.2。
2. 两个 RKNN 已通过 Toolkit2 模拟器，但没有真实 RK3588 NPU 测试结果。
3. 手持模型构建日志出现三条 `Unkown op target: 0`，构建、导出、重新加载和模拟推理仍均成功；真实板端必须重点验证。
4. 不同厂家系统镜像的 RKNPU 驱动版本可能不同。
5. 部分板端 OpenCV 不支持 `mp4v` 写出，可能需要更换编码器或使用 FFmpeg。
6. 商品名称依赖固定机位和固定 ROI。
7. `-3`、`-2` 是演示时间线，不是完全自动的商品计数结果。
8. 双模型加手持切片推理的真实 FPS 只能在 RK3588 上测量。

## 十二、遇到问题时需要回传的信息

请一次性提供：

- 开发板品牌和完整型号；
- `cat /etc/os-release`；
- `uname -a`；
- `diagnostics/rk3588_probe.json`；
- `python3 --version`；
- Lite2 wheel 完整文件名；
- `librknnrt.so` 版本；
- RKNPU 驱动信息；
- 两份转换日志；
- `./scripts/run_demo.sh` 完整终端输出；
- 生成的视频，或失败时最后一帧截图。

这些材料可以帮助快速区分系统、驱动、版本、模型、后处理、视频编码或业务配置问题。
