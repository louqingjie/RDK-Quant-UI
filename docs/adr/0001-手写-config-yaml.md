# ADR-0001：config.yaml 手写，PyYAML 只进 `dev`

- **状态**：已接受
- **日期**：2026-10-03

## 问题

M4 要生成 `hb_mapper makertbin` 的 `config.yaml`。结构固定：四个 section、
约 20 个标量键，全部是 ASCII 字符串 / 布尔 / 整数。

项目原先的计划写在 `pyproject.toml` 里：

> core 是**纯标准库**实现（urllib / subprocess / ctypes），刻意不引入
> requests、psutil。**onnx 与 PyYAML 会在配置与模型内省模块落地时加入。**

也就是说，按原计划这一步就该把 PyYAML 加进运行时依赖。

## 选择

**运行时手写 YAML，`core/` 保持零依赖；PyYAML 只加进 `dev` extra，在测试里
解析我们写出的文本、断言结构。**

（同一次改动里 `onnx` 没有加 —— M4 不需要它。模型的形状内省走容器侧探针，
见 `core/probe.py`。）

## 替代方案

**运行时用 PyYAML**（即原计划）：

- 代码更少，`yaml.safe_dump` 一行搞定
- 不用自己处理 quoting

**没选它的原因**：`AGENTS.md` 的项目说明里写着「`core/` 是纯标准库」。
加了运行时依赖那句话就得改，而它的价值是实打实的 —— 这个包要在
OpenExplorer 容器（Ubuntu 20.04 / Python 3.8）里装得进去，少一个依赖少一份
版本冲突的可能。

**完全不用 PyYAML（测试里也不用，改造断言字符串）**：

- 零依赖做得更彻底

**没选它的原因**：断言字符串相等会把测试绑到**格式**上 —— 一次缩进调整就会
把测试弄红而行为没变。用 PyYAML 解析后断言结构，断的是**含义**。

## 代价

**手写要自己处理 quoting。** 唯一的真实风险在 `output_model_file_prefix` ——
它来自用户的 ONNX 文件名，什么都可能有。做法是：

- 所有字符串**一律加单引号**，内部的 `'` 写成 `''`（YAML 的规定）
- 单引号标量表示不了换行，遇到就 `ValueError`，**宁可当场拒也不写出坏文件**

代价的缓解是一条参数化测试：`"有'单引号"`、`"有: 冒号"`、`" 前后有空格 "`、
`"emoji🙂"` 等九种输入，都由 PyYAML 读回来逐字比对。

另一个代价：**schema 变复杂时手写会开始痛**。目前四个 section 都是扁平的，
一旦要写 `custom_op` 那种嵌套结构，这段代码就该重新评估 ——
那时换成运行时 PyYAML 是合理的，本 ADR 也随之作废。

## 后果

- `pyproject.toml` 的 `dependencies` 保持 `[]`，`dev` 变成
  `["pytest>=8.0", "PyYAML>=6.0"]`
- 两个平台的虚拟环境都要装 PyYAML（`scripts/dev-setup.sh` 走 `.[dev]`，本来就覆盖）
- `pyproject.toml` 里那句「PyYAML 会在配置模块落地时加入」**已改**，
  改成指向本 ADR
