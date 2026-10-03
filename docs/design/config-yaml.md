# config.yaml 的真实 schema

S4 要写的配置文件。**不是照官方文档抄的** —— 是从镜像里的 schema 定义挖出来的。
来源（X5 CPU `v1.2.8-py310`）：

    horizon_tc_ui/config/mapper_conf_parser.py     conf_schema_dict —— 字段、类型、默认值
    horizon_tc_ui/config/mapper_consts.py          各枚举的合法取值
    horizon_tc_ui/config/fast_perf_template.yaml   官方模板

⚠️ `hmct/tools/create_quant_config.py`（官方生成器）**在这个镜像里是坏的**：
它只 `shutil.copy` 一份 `hmct/common/quant/quant_config.json`，而那个文件
**不存在**。不能靠它生成配置。

## 顶层结构

四个 section。必填与否由 `_validate_*` 单独判定，**标 ★ 的是实际必需的**。

### `model_parameters`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| ★ `march` | str | 无 | **必填**。取值见下，X5 用 `bayes-e` |
| ★ `onnx_model` | str | 无 | 容器内相对路径 |
| `working_dir` | str | **`./model_output`** | ⚠️ 见下面的坑 |
| `output_model_file_prefix` | str | `model` | 产物文件名的前缀 |
| `log_level` | str | | |
| `layer_out_dump` | bool | `False` | |
| 其余 `output_nodes` / `remove_node_*` / `set_node_data_type` / `debug_mode` / `node_info` | str \| dict | | 调优用，本项目暂不写 |
| `caffe_model` / `prototxt` | str | | Caffe 用。本项目只支持 ONNX |

### `input_parameters`

| 键 | 类型 | 说明 |
|---|---|---|
| ★ `input_name` | str | 模型的输入名（S1 从 ONNX 读出来） |
| ★ `input_type_train` | str | **推不出来**，必须用户确认（见 `ui/pages/model.py` 的说明） |
| ★ `input_layout_train` | str | 同上 |
| ★ `input_type_rt` | str | 板端输入类型 |
| `input_layout_rt` | str | **`input_type_rt` 是 `nv12` 时不要写** —— 写了打 WARNING「is not needed」 |
| `input_shape` | str | ASCII `x`，例如 `1x3x640x640` |
| `norm_type` | str | 校准数据不做归一化时用 `no_preprocess` |
| `input_batch` / `mean_value` / `scale_value` | str | |

⚠️ **`input_source` 不在这里**，它在 `compiler_parameters` 下（见下）。

### `calibration_parameters`（默认 `{}`）

| 键 | 类型 | 说明 |
|---|---|---|
| `cal_data_dir` | str | 校验是 `And(str, len)` —— **不允许空字符串** |
| `calibration_type` | str | |
| `cal_data_type` | str | |
| `preprocess_on` | bool | 保持 `False`（见 [`container-findings.md`](container-findings.md) 第 3 节） |
| `per_channel` | bool | |
| `max_percentile` | float | |
| `run_on_cpu` / `run_on_bpu` | str | |
| `enable_int16` | bool | |
| `optimization` | str | |

### `compiler_parameters`（默认 `{}`）

| 键 | 类型 |
|---|---|
| `compile_mode` / `optimize_level` / `ability_entry` | str |
| `balance_factor` / `core_num` / `max_time_per_fc` / `jobs` / `advice` | int |
| `debug` | bool |
| `input_source` | str \| dict |

**`input_source` 有两层坑**：

1. **不能写进 `input_parameters`** —— 直接解析失败，而且报错不提示该去哪：

       ERROR Key 'input_parameters' error:
       Wrong key 'input_source' in {'input_name': 'images', ...}

   它在 `compiler_parameters` 下。

2. **必须是 dict，不能是字符串** —— schema 写的是 `Or(str, dict)`，
   但运行期只认 dict：

       ERROR Invalid input_source format received. input_source should be a dict

   正确写法是**按输入名映射**：

   ```yaml
   compiler_parameters:
     input_source:
       images: 'pyramid'
   ```

   合法值见下面的 `input_source_support_dict`。**不写**也行 —— 工具会打一条
   WARNING 然后按 `pyramid` 处理。

## 合法取值（`mapper_consts.py` 实测）

```
march_list          = ['bernoulli2', 'bayes', 'bayes-e']
                      J5:bayes   X5:bayes-e   XJ3:bernoulli2
cal_data_type_list  = ['uint8', 'float32', 'int32', 'int16', 'int8']
layout_list         = ['NHWC', 'NCHW']
autoq_caltype_list  = ['kl', 'max', 'mix', 'default']      自动量化
preq_caltype_list   = ['load', 'skip']                     预量化
norm_type_list      = ['no_preprocess', 'data_mean', 'data_scale',
                       'data_mean_and_scale', 'mean_file'(废弃), 'mean_file_and_scale'(废弃)]
NCHW_types          = ['rgbp', 'bgrp', 'yuv444p', 'gray', 'featuremap']
NONE_types          = ['nv12', 'yuv420sp_bt601_video']     这些不需要 layout

input_source_support_dict = {
    'pyramid':  ['nv12', 'gray', 'yuv420sp_bt601_video', 'yuv_bt601_full'],
    'ddr':      ['rgb', 'bgr', 'yuv444', 'yuv444_128', 'gray', 'featuremap'],
    'resizer':  ['nv12', 'gray', 'yuv420sp_bt601_video', 'yuv_bt601_full'],
}
```

**`march` 的默认值是 `bayes`（J5），不是 `bayes-e`。** 这和
`hb_mapper checker` 是同一个坑（根 [`AGENTS.md`](../../AGENTS.md) 第 8 条）：
不显式传就量化错芯片。

## 命令行

    hb_mapper makertbin --config config.yaml --model-type onnx

**`--model-type onnx` 是必须的。** 不传会失败：

    2026-10-03 19:44:26,757 ERROR wrong model type None

而且它**不会**从配置里的 `onnx_model` 推断（我们那份配置里明明有 `onnx_model`，
照样报这个错）。

**`DockerBackend.compile` 已经写对了** —— `backends.py:177-182` 里
`--model-type onnx` 就在 `--config` 前面。这条是给**手工敲命令**和
**将来改这段代码的人**看的。

## 两个坑（实测）

**1. `working_dir` 的默认值是 `./model_output`，不是 `./`。**

我们的工作区用的是 `output/`（`core/backends.py` 的 `OUTPUT_DIR`）。
不显式写 `working_dir: './output'` 的话，产物会落在 `model_output/`，
而我们的产物发现逻辑扫不到。

**2. `input_type_rt = nv12` 时不要写 `input_layout_rt`。**

    2026-10-03 19:43:42,241 WARNING Nv12 input layout info received.
      input type rt '0' is nv12 and layout 'NHWC' is not needed.

`nv12` 属于 `NONE_types`，它自带排布。

## `cal_data_dir` 的后缀启发式（实测确认）

    2026-10-03 19:43:42,244 INFO The calibration dir name suffix is the same as
      the value float32 of the cal_data_type parameter and will be read with
      the value of cal_data_type.

所以 `calib_bin_f32` / `calib_bin_u8` 这个命名（`CalibBuildSpec.output_dir_name`）
是对的，工具会照它推断类型。**不匹配时会告警，但不会失败。**

## 跑起来是什么样

耗时、真实产物名、失败方式、以及**没确认的两件事**，都在
[`makertbin-runtime.md`](makertbin-runtime.md)。

一句话预告：**校准很慢**（CPU 镜像上每张样本约 164 秒），
所以别拿几百张样本做探索。

