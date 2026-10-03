# `hb_mapper makertbin` 的运行行为

**实测**出来的：耗时、产物名、失败方式。schema 在
[`config-yaml.md`](config-yaml.md)，这里只讲**跑起来是什么样**。

## ⚠️ 校准耗时：CPU 镜像上不可忽略

X5 **CPU** 镜像，`0708.onnx`（YOLO，输入 1×3×640×640）：

    calibration in progress: 10/380 [25:00<16:49:12, 163.65s/it]

**每张样本约 164 秒**，而 `calib_bin_f32` 里有 **380** 张 → 预计 **17 小时**。

**这不是「自动搜索慢」。** 380 是**样本数**，任何 `calibration_type` 都要跑
这么多张；`default` 只是把它们按参数组合分组：

    The activation calibration parameters:
        calibration_type:     ['max', 'kl']
        max_percentile:       [0.99995, 1.0]
        per_channel:          [True, False]
        asymmetric:           [True, False]
    The modelwise search parameters:
        similarity:           0.995
        metric:               cosine-similarity

**对 S4 的含义**：开始转换前应当把「样本数 × 单张耗时」估出来给用户看，
否则点一下要等一整晚。官方建议的 20~100 张不只是「代表性」问题，
也是**时间**问题。

## 真实产物名

    output/0708_original_float_model.onnx     5648 KB
    output/0708_optimized_float_model.onnx    9185 KB

**与 `CONVERSION_ARTIFACTS` 的五个模式逐字对得上** ✓ 命名规则是
`<output_model_file_prefix>_<阶段>_model.onnx`。

## 失败也会往 `output/` 里写东西

`0526.onnx` 那次因类型不匹配失败，留下了 `output/shape_inference_fail.onnx`
（4.2 MB）。所以**每次转换前要清空 `output/`** —— 否则「5 个产出物齐全」
会被上一次的残留喂出假通过。

## 两种模型失败方式

**1. ONNX 输入是 FLOAT16 而图里是 FLOAT。**

    0526.onnx: Graph input: images: shape=[1, 3, 640, 640], dtype=FLOAT16
    ERROR-OCCUR-DURING {horizon_nn.build_onnx}:
      [ShapeInferenceError] (op_type:Conv, node name: /m/model.0/conv/Conv):
      [TypeInferenceError] Inferred elem type differs from existing elem type:
      (FLOAT) vs (FLOAT16)

**`hb_mapper checker` 不会拦下这种模型** —— 它对同一个模型报的是
`dtype=FLOAT32` 并跑完了。也就是说**模型能过 S2、却在 S4 才炸**，而报错里
一个中文字都没有。S4 的错误分类要把这类认出来（「模型输入是 float16，
本工具链要求 float32」），而不是把原文甩给用户。

**2. `0708.onnx` 正常** —— 走完 shape inference、优化，开始校准。

## 没确认的两件事

以下两条本打算靠一次完整运行确认，但**校准要 17 小时**，没跑到底：

1. **控制台成功字样的确切文本** —— 不知道 makertbin 结束时会打什么
2. **余弦相似度表会不会被打印出来** —— 相似度是自动搜索的**内部指标**
   （见上面的 `metric: cosine-similarity`），但它是否以**表格**形式出现在
   控制台，没验证

`core/backends.py` 的 `_MOCK_COMPILE` 用的是 `[SUCCESS] 转换完成`、
`[SUCCESS] 输出: ...` —— **那是照什么写的无法确认**。

**对 S4 的含义**：实现时要么用一次短校准（`calibration_type: skip` 或极少
样本）把结尾确认掉，要么把「成功字样」的判断写成**不依赖具体措辞**
（例如「退出码为 0」+「5 个产物齐全」），而不是硬编码字符串。

## 怎么复现这些测量

工作区要有一份 `config.yaml`（schema 见 [`config-yaml.md`](config-yaml.md)），
然后：

    docker run --rm -v <工作区>:/workspace --workdir /workspace \
      --shm-size 4g <镜像> hb_mapper makertbin --config config.yaml --model-type onnx

**别用 `calibration_type: default` 加几百张样本做探索** —— 那是十几小时。
要快速看结尾行为，用 `calibration_type: skip`。
