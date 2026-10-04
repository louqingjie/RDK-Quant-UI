# `hb_mapper makertbin` 的运行行为

**实测**出来的：耗时、产物清单、成功字样、失败方式。schema 在
[`config-yaml.md`](config-yaml.md)，这里只讲**跑起来是什么样**。

## 两条耗时基线

| 情况 | 用时 |
|---|---|
| `calibration_type: skip`，`0708.onnx`（YOLO 1×3×640×640）**跑完** | **87 秒** |
| `max`，5 张样本 | 0.2 秒/张（**4.89 it/s**） |
| `default`，5 张**好图** | 2–3 秒/张 |
| `default`，那组**坏图**（见下） | **168 秒/张** |

## ⚠️ 校准耗时：**和校准图强相关，差 60 倍**

同一个模型、同一台机器（i5-10400，容器拿到 12 核、无 cgroup 配额）、
同样的 5 张样本数，**只换校准图**：

| 校准图 | `max` | `default` |
|---|---|---|
| `0002,0011,0053,0058,0065`（按文件名取的前 5 张） | 0.2 s/张 | **168 s/张** |
| `2913,3412,0177,3127,3278`（另一组） | — | 2.7 s/张 |
| `0011,0053,0058,0065`（上一组去掉 0002） | — | 2.7 s/张 |
| `0002,0894,2714,0769,1600`（最暗的 5 张） | — | 2.7 s/张 |
| `0894` + 4 张正常 | — | 2.7 s/张 |

**排除掉的假设**（都做了对照）：不是样本数（5 张和 380 张的单张成本一样）、
不是机器被抢（空闲机器上复现）、不是暗图、不是明暗混合、不是图片相关性、
不是 `default` 本身（好图上它只比 `max` 慢 10 倍）。

**唯一站得住的描述**：那一组图会触发 `default` 的病态路径。机制在
`pyquantizer.so` 里，看不到。

### 为什么搜索类校准会这样 —— 官方 FAQ 给出了机制

`default` 是**分步升级**的（[官方 FAQ 6.6.1.4](https://developer.d-robotics.cc/api/v1/fileData/x5_doc-v126cn/oe_mapper/source/ptq/ptq_faq_troubleshooting/ptq_faq.html)）：

| 步骤 | 做什么 | 进入下一步的条件 |
|---|---|---|
| Step1 | 试 Max / Max-P 0.99995 / KL | **最高余弦相似度 < 0.995** |
| Step2 | 加 perchannel 组合 | **还是 < 0.995** |
| Step3 | 再加非对称量化，5 种里选最佳 | — |

`mix` 是**逐节点**的（[6.6.1.5](https://developer.d-robotics.cc/api/v1/fileData/x5_doc-v126cn/oe_mapper/source/ptq/ptq_faq_troubleshooting/ptq_faq.html)）：

| 步骤 | 做什么 |
|---|---|
| Step1 | 用 KL 算**每个节点的量化敏感度** |
| Step2 | **遍历所有敏感节点**，每个试 Max / Max-P / KL |
| Step3 | 评估 4 种模型的累积误差，输出最优 |

**数据越"难"，升级越深、敏感节点越多，成本成倍涨。**
喂一堆异常样本就是在主动触发最贵的路径 —— 所以这不是"运气不好"，
是**输入不合规的必然后果**。

### 官方对校准集的要求（我们违反了两条）

[X5 手册 6.3.3](https://developer.d-robotics.cc/api/v1/fileData/x5_doc-v126cn/oe_mapper/source/ptq/ptq_usage/prepare_calibration_data.html)：

> 校准阶段会需要 **20~100 份**的标定样本……**不要使用非常少见的异常样本，
> 例如纯色图片、不含任何检测或分类目标的图片等**。

社区实践（引 Model Zoo 的警告阈值）：**20 张起步；>50 张校准会很慢**。

**我们此前是 380 张（超 3.8 倍）、且未经任何筛选** —— 两条都不满足。

### 对 S4 / S3 的含义

1. **不要在跑之前承诺时间。** 曾经给过「每张约 164 秒」，而好图上是 2.7 秒 ——
   **错 60 倍**，方向还不确定。真实速度由 `hb_mapper` 自己的进度条在跑时报出来。
2. **S3 要按官方口径挑图**：`core/calibpick.py` 做去重 + 剔纯色 + 按亮度分层，
   界面上的「自动挑选」按钮跑它。
3. **超过 50 张就要提醒**（`calibpick.SLOW_THRESHOLD`）。

**探索时用 `skip`** —— 87 秒 vs 十几小时。

## ⭐ stdout 会折行，工作区日志**不会**

**这条决定了 S4 怎么解析。**

同一个节点表，**stdout** 是这样的（在 120 列处硬折行）：

    Node                              ON   Subgraph  Type              Cosine Similarity  Th
    reshold  DataType
    ---------------------------------------------------------------------------------------
    -------------------
    HZ_PREPROCESS_FOR_images          BPU  id(0)     HzSQuantizedPreprocess  --          12
    0        int8

而工作区里的 `hb_mapper_makertbin.log` 是**完整的**：

    Node                              ON   Subgraph  Type              Cosine Similarity  Threshold  DataType
    HZ_PREPROCESS_FOR_images          BPU  id(0)     HzSQuantizedPreprocess  --          1.0        int8

实测同一轮：stdout **601 行**，日志文件 **352 行**（折行让 stdout 多了 249 行）。

**所以 `parse_makertbin` 要读日志文件，不读 stdout。**
`core/logparse.py` 的 `strip_log_prefix` 已经在处理这个格式
（`file: X func: Y line No: N` 前缀）—— `parse_checker` 走的也是日志文件，
M4 照同一条路走。

日志文件**总是会写**，第一行就说明位置：

    INFO log will be stored in /workspace/hb_mapper_makertbin.log

它只在运行**结束后**才完整，所以「边跑边看」用 stdout、「跑完解析」读日志文件。

## ⭐ 成功字样

日志文件的**最后两行**，都是 `INFO` 级：

    ... file: hb_mapper_makertbin.py func: hb_mapper_makertbin line No: 783 Convert to runtime bin file successfully!
    ... file: hb_mapper_makertbin.py func: hb_mapper_makertbin line No: 784 End Model Convert

**`core/backends.py` 的 `_MOCK_COMPILE` 写的是 `[SUCCESS] 转换完成` / `[SUCCESS] 输出: ...`
—— 和现实不符**，见下面「Mock 与现实的三处偏差」。

## 完整产物清单

`calibration_type: skip` 跑完，`output/` 里有 **8 个**文件：

    0708_original_float_model.onnx     5648 KB   ← CONVERSION_ARTIFACTS ✓
    0708_optimized_float_model.onnx    9185 KB   ← ✓
    0708_calibrated_model.onnx         9443 KB   ← ✓
    0708_quantized_model.onnx          2728 KB   ← ✓
    0708.bin                           2113 KB   ← ✓（模式是 *.bin）
    0708_quant_info.json                 91 KB   ← 不在五个模式里
    torch_jit_subgraph_0.html          2206 KB   ← 不在
    torch_jit_subgraph_0.json             5 KB   ← 不在

**`CONVERSION_ARTIFACTS` 的五个模式逐字成立** ✓ 但 `output/` 里**还会有别的东西**，
所以「5 个产出物」是一项**存在性**检查，不能要求目录里只有那五个。

命名规则：`<output_model_file_prefix>_<阶段>_model.onnx`，bin 就是
`<output_model_file_prefix>.bin`（**没有** march/尺寸/nv12 那些后缀）。

## 失败也会往 `output/` 里写东西

`0526.onnx` 因类型不匹配失败，留下 `output/shape_inference_fail.onnx`（4.2 MB）。
所以**每次转换前要清空 `output/`**，否则「5 个产出物齐全」会被上次残留喂出假通过。

## 两种模型失败方式

**1. ONNX 输入是 FLOAT16 而图里是 FLOAT。**

    0526.onnx: Graph input: images: shape=[1, 3, 640, 640], dtype=FLOAT16
    ERROR-OCCUR-DURING {horizon_nn.build_onnx}:
      [ShapeInferenceError] (op_type:Conv, node name: /m/model.0/conv/Conv):
      [TypeInferenceError] Inferred elem type differs from existing elem type:
      (FLOAT) vs (FLOAT16)

**`hb_mapper checker` 不会拦下这种模型** —— 它对同一个模型报的是 `dtype=FLOAT32`
并跑完了。也就是**能过 S2、却在 S4 才炸**，而报错里一个中文字都没有。
S4 的错误分类要把这类认出来，而不是把原文甩给用户。

**2. `0708.onnx` 正常** —— 87 秒跑完。

## 余弦相似度列：`--` 是什么意思

节点表**总是会打印**（196 个节点行），`Cosine Similarity` 一列填什么取决于有没有校准：

- `calibration_type: skip` / `load` → **全是 `--`**（实测 196/196 都是）
- 真校准过 → 应该是数字

`hmct/reporter/print_model_info.py` 里写得清楚：先填 `"--"`，只有节点在
`node_similarity_info` 里才替换成数值。

**对「余弦相似度表」这项校验的含义**：表存在 ≠ 有数值。`skip` 模式下这项**应当
只校验「表被解析到了」**，不要因为全是 `--` 判失败 —— 那是`skip` 的正常结果。

## Mock 已经和实测对齐

`core/backends.py` 的 `MockBackend` 原先有三处和实测不符，**M4 已修**，
`tests/test_backends.py` 里有契约测试守着：

| | 原先（编的） | 现在（实测） |
|---|---|---|
| 成功字样 | `[SUCCESS] 转换完成` | `Convert to runtime bin file successfully!` |
| 产物 | 只写 `.bin` | 5 个都写 |
| bin 名字 | `{stem}_{tag}_640x640_nv12.bin` | `{prefix}.bin` |

另外 Mock 现在**会写 `hb_mapper_makertbin.log`**。这一条比上面三条更要紧：
没有它，S4 的解析路径在 mock 下走的是"日志读不到"那条分支，**等于没测**。
假日志的节点表也照抄了"跨词表头"的形状，否则列错位那个 bug 在 mock 下复现不出来。

## 端到端实测：走的是界面那条路

M4 做完后用 `core/conversion.py` + `DockerBackend` 真跑了一遍 ——
**和 `ConvertPage._on_run()` 逐行相同**，只少了 Qt 那一层：

    clear_output(output/) → write_config(config.yaml) → backend.compile()
      → read_log(hb_mapper_makertbin.log) → parse_makertbin()
      → discover_artifacts(output/) → evaluate_conversion()

结果（`0708.onnx`，`calibration_type: skip`）：

    onnx_model = ./model/0708.onnx        ← 实测过的相对形式
    清空 output/：8 个文件
    compile rc = 0
    日志文件 352 行
    saw_success=True  saw_end=True  表=True  节点=196  ERROR 行=0
    产物 5/5：0708.bin 2113KB / calibrated 9442KB / optimized 9184KB
              original 5648KB / quantized 2728KB
    三项校验：{'控制台成功字样': True, '余弦相似度表': True, '5 个产出物': True}
    ok = True

**`config.yaml` 里的路径用相对形式（`./model/x.onnx`）。**
`Workspace.rel()` 给的是绝对形式（`/workspace/model/x.onnx`），两种容器里都能用，
但相对形式才是官方写法、也才是这里验证过的 —— 所以加了 `Workspace.rel_dot()`。

## 怎么复现这些测量

工作区放一份 `config.yaml`（schema 见 [`config-yaml.md`](config-yaml.md)），然后：

    docker run --rm -v <工作区>:/workspace --workdir /workspace \
      --shm-size 4g <镜像> hb_mapper makertbin --config config.yaml --model-type onnx

**解析产物要看 `output/`，解析结果要看 `hb_mapper_makertbin.log`，别看 stdout。**
