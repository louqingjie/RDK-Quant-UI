# 容器实测结论

这里的每一条都是**在真实 OpenExplorer 容器里跑出来的**，不是从文档推断的。
记录它们是因为下面几条都推翻过或差点推翻我们的实现。

镜像：`openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8-py310`

---

## 1. `cal_data_dir` 的真实格式：**无文件头的原始二进制**

2026-10 用微型 Conv 模型（输入 `1x3x64x64`）做的对照实验，同一批像素两种存法：

| 目录 | 存法 | 结果 |
|---|---|---|
| `calib_npy/` | 3 个 `.npy`（`np.save`，每个 49280 字节） | **退出码 255 失败** |
| `calib_bin_f32/` | 3 个 `.bin`（裸 float32，每个 49152 字节） | **退出码 0 成功**，产出 `tiny_bin.bin` |

`.npy` 的报错是决定性的：

```
ERROR load cal data for input 'images' error
ERROR cannot reshape array of size 12320 into shape (1,3,64,64)
```

`12320 = 12288 + 32`。12288 是 `1×3×64×64` 的元素数，32 正是 **`.npy` 的 128 字节文件头**
（128 ÷ 4 = 32）被当成 float32 读出来的结果。也就是说工具**把文件当裸字节读**，
按 `cal_data_type` 重新解释，**不做任何解析、不接受任何头部**。

### ⚠️ 一个容易踩的坑

`hmct/common/data/calibration_data_set.py` 里的 `Dataset._load_data_from_path`
**确实**用 `np.load()` 逐个读文件。但那是**Python API 那条入口**，不是 YAML 里
`cal_data_dir` 的那条——后者在编译好的 `pyquantizer.so` 里实现。

**看到那段源码就以为 YAML 要 `.npy` 是错的。** 必须以对照实验为准。

### 对实现的约束

- 产物必须是**裸字节**，长度**恰好** `W×H×C×itemsize`，多一个字节都不行
- 这也给了我们一个硬校验：字节数不对就一定读不了，可以在生成前就报出来
- 不要"优化"成 `.npy`

## 2. 目录名的 `_f32` / `_u8` 后缀是真的，而且会告警

```
calib_npy/     WARNING  The calibration dir name suffix is not the same as the
                        value float32 of the parameter cal_data_type,
                        the parameter setting will prevail
calib_bin_f32/ INFO     The calibration dir name suffix is the same as the value
                        float32 of the cal_data_type parameter and will be read
                        with the value of cal_data_type.
```

所以生成的目录名定为 `calib_bin_f32` / `calib_bin_u8`，让官方启发式与显式的
`cal_data_type` 一致，不产生告警。**同时始终显式写出 `cal_data_type`**，不依赖后缀。

## 3. 校准数据不做归一化

`cal_data_dir` 里的值被直接 reshape 成输入形状喂给模型。模型的 `norm_type`
预处理是**被工具插入到模型里**的，所以校准数据应当是 `input_type_train` 格式的
**原始像素**。这与官方文档一致，也与实验一致（`norm_type: no_preprocess` 下通过）。

探针的 `calib-build` 默认不叠加 mean/scale，mean/scale 只在显式给出时才应用。

## 4. `hb_mapper checker` 的真实行为

- 命令行：`--proto` / `--model` / `--model-type [caffe|onnx]`（必填）/
  `--march [bernoulli2|bayes|bayes-e]` / `--input-shape <TEXT TEXT>...` / `--output`（已废弃）
- **`--march` 默认是 `bayes`，不是 X5 需要的 `bayes-e`** → 必须每次显式传
- 日志落在**工作目录**的 `hb_mapper_checker.log`。`--help` 里写的是
  `hb_mapper_checker_{date_time}.log`，**帮助文本是过时的**，实际没有日期后缀
- 输出格式（见 `checker_real.log`）：行首是
  `时间戳 file: x.py func: y line No: N`；节点表表头
  `Node ON Subgraph Type Cosine Similarity Threshold DataType`；
  **`ON` 列装的是 BPU/CPU**
- 表格后面紧跟 `The quantized model output:` —— 它的列数恰好也落进"列数区间"，
  所以行过滤必须靠"设备列必须是 BPU/CPU"，不能只靠列数
- 控制台输出带 ANSI 颜色码，日志文件里没有

## 5. 容器环境（X5 CPU v1.2.8）

```
python 3.10.15 / onnx 1.12.0 / numpy 1.23.0
图像库：PIL、skimage、cv2 三个都有
hb_mapper 1.24.3 / hbdk 3.49.15 / horizon_nn 1.1.0
```

`import horizon_nn` 解析到的是 `/usr/local/lib/python3.10/dist-packages/hmct`
（Horizon Model Convert Toolkit）。

**X5 是 3.10，不是 3.8。** 探针仍然保持 3.8 兼容——X3 容器未验证。

## 6. 镜像名只能从 `docker load` 输出解析

```
Loaded image: openexplorer/ai_toolchain_ubuntu_20_x5_cpu:v1.2.8-py310
```

真实 tag 是 **`v1.2.8-py310`**，清单里原先猜的 `v1.2.8` 是错的。
"不要硬编码镜像名"这条决定救了我们。

## 7. 容器必须以 **root** 运行（不传 `--user`）

⚠️ **这一节曾经写反过。** 原文是「容器必须以 `--user` 运行」，理由是
不传会留下 root 所有的文件。那个理由本身没错，但被一个更硬的事实推翻了：

```
ModuleNotFoundError: No module named 'torch'
```

`hmct/ir/onnx_graph.py:21` **无条件** `import torch`。而 torch 装在
`/root/.local/lib/python3.10/site-packages/`，`/root` 的权限是 **700**。
所以下面这些全都救不回来：

| 尝试 | 结果 |
|---|---|
| `--user 1000:1000` | 进不去 `/root`，`import torch` 失败 |
| `-e HOME=/root` | 权限仍是 700，无效 |
| `-e PYTHONPATH=/root/.local/...` | 目录不可读，无效 |

**结论：必须 root。** 文件属主问题改用**事后按需 chown** 处理
（`runner.py` 的 `fix_ownership`，只在工作区里发现 root 所有的文件时才动，
且是 best-effort，不影响退出码）。

代价：偶尔会多一次 chown 的往返；WSL 下如果用户在宿主机没有 sudo，
chown 可能失败——那种情况只记日志，不阻断。

**教训**：文档写的和代码相反时，先假设**代码是对的**（它经过了实测），
然后去把文档改对，而不是照着文档去改代码。

## 8. 容器里有一批可复用的官方工具

`hmct/tools/`：`print_calibration_info.py`、`visualizer.py`、`profiler.py`、
`compatibility.py`、`compare_calibrated_and_quantized_model.py`、
`compare_original_and_optimized_model.py`、`simplify.py`、`convert.py`、
`create_quant_config.py`、`debug.py`、`libinfo.py`

M4/M5 应优先调用这些，而不是自己解析 `hb_perf` 的 HTML 输出。
