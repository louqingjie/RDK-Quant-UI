# `hb_mapper makertbin` 的运行行为

**实测**出来的：耗时、产物清单、成功字样、失败方式。schema 在
[`config-yaml.md`](config-yaml.md)，这里只讲**跑起来是什么样**。

## 两条耗时基线

| 情况 | 用时 |
|---|---|
| `calibration_type: skip`，`0708.onnx`（YOLO 1×3×640×640）**跑完** | **87 秒** |
| `calibration_type: default`，同模型 | 每张样本 **164 秒** × 380 张 ≈ **17 小时** |

    calibration in progress: 10/380 [25:00<16:49:12, 163.65s/it]

**380 是样本数，不是「自动搜索的次数」。** 任何 `calibration_type` 都要跑这么多张；
`default` 只是把它们按参数组合分组：

    The activation calibration parameters:
        calibration_type:     ['max', 'kl']
        max_percentile:       [0.99995, 1.0]
        per_channel:          [True, False]
        asymmetric:           [True, False]
    The modelwise search parameters:
        similarity:           0.995
        metric:               cosine-similarity

**对 S4 的含义**：开始转换前要把「样本数 × 单张耗时」估出来给用户看，否则点一下
要等一整晚。官方建议的 20~100 张**不只是代表性，也是时间问题**。

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

## Mock 与现实的三处偏差

`core/backends.py` 的 `MockBackend` / `_MOCK_COMPILE` 有三处和实测不符，
M4 要一起修：

| | Mock 现在 | 实测 |
|---|---|---|
| 成功字样 | `[SUCCESS] 转换完成` | `Convert to runtime bin file successfully!` |
| 产物 | 只写 `.bin` | 5 个产物都写 |
| bin 名字 | `{stem}_{tag}_640x640_nv12.bin` | `{prefix}.bin` |

## 怎么复现这些测量

工作区放一份 `config.yaml`（schema 见 [`config-yaml.md`](config-yaml.md)），然后：

    docker run --rm -v <工作区>:/workspace --workdir /workspace \
      --shm-size 4g <镜像> hb_mapper makertbin --config config.yaml --model-type onnx

**解析产物要看 `output/`，解析结果要看 `hb_mapper_makertbin.log`，别看 stdout。**
