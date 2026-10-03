# Qt 陷阱

改 `src/rdkquant/ui/` 之前扫一眼。**每条都真实踩过**，写的不是"可能发生"，是"已经发生"。

根文件 [`AGENTS.md`](../AGENTS.md) 只列名字，这里放具体机制和怎么发现。

---

## 1. `processEvents()` 不处理 `DeferredDelete`

`deleteLater()` 只是把删除**排队**，而普通的 `processEvents()` **不处理
`DeferredDelete` 事件**——那个队列永远不会被执行。

**症状**：测试里控件越积越多。`app.topLevelWidgets()` 一轮涨 4 个、从不回落。
后果不只是内存——每次 `apply_theme()` 里的 `setStyleSheet()` 都要重新 polish
**所有**顶层控件，于是耗时随用例数线性增长：

```
第 1 轮  apply_theme     2.2 ms   顶层控件 4
第 2 轮  apply_theme   376.0 ms   顶层控件 8
第 4 轮  apply_theme  1182.5 ms   顶层控件 16      ← 增长 589 倍
```

**修法**（Qt6 里 `ProcessEventsFlag.DeferredDeletion` 已经移除）：

```python
window.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
qapp.processEvents()
```

对照实验（判据：`apply_theme` 耗时是否平稳）：

| 方式 | 增长 |
|---|---|
| `close + deleteLater + processEvents()` | 589x ❌ |
| `+ sendPostedEvents(DeferredDelete)` | 0.0x ✅ |
| `shiboken6.delete()` 立即销毁 | 1.1x ✅ |

**怎么发现的**：测试套件慢到 238 秒，`--durations` 显示每个 UI 用例的 setup 要 8~12 秒。
先怀疑图标缓存，量出来是 0.2 ms（不贵）；再量"界面到底有没有被销毁"，才发现根本没销毁。

修完 `test_ui_smoke` 从 169 秒到 9.26 秒，全量 238 → 16 秒。

**连带教训**：之前一直用 `icons.clear_cache()` 压一个段错误，自以为那是"必要代价"
（还写进了提交信息：套件从 60 秒变 170 秒）。那其实是同一个累积问题的另一个症状。

---

## 2. `QListView` 在 IconMode 下只读模型的第 0 列

用 `setItem(row, col)` 铺多列，**只有每行第一个会被画出来**，不报错。

**症状**：50 张缩略图只显示了一排 9 个。`50 ÷ 6 列向上取整 = 9` —— 那 9 个正是每行的第一个。

**修法**：全部 `appendRow` 到第 0 列，换行交给视图按宽度自己流式排。

高度也得自己算（外层是滚动区，视图给不出合适的 `sizeHint`）：

```python
per_row = max(1, self.viewport().width() // self.gridSize().width())
rows = max(1, (count + per_row - 1) // per_row)
self.setMinimumHeight(rows * gridSize.height() + 12)
```

`resizeEvent` 里要重算。

---

## 3. `make_table(headers)` 收的是表头列表，不是 model

传错**不会抛异常**，只会得到一张**全 0 宽的空表**。

原因链：model 没设上 → `columnCount() == 0` → `fit_columns_to_content` 提前返回
→ 所有列宽为 0 → 什么都看不见，而且没有任何报错。

用 `StagePage.make_table_with(headers)`，它保证 `(model, view)` 正确接上。

---

## 4. 信号连接要小心「立即求值」

```python
("反选", self.grid.invert_checked)     # ← 立即求值，此时 self.grid 还不存在
```

放进 tuple/列表时会**当场取值**，而控件可能几行之后才创建 → 构造期 `AttributeError`。

包成 lambda：`("反选", lambda: self.grid.invert_checked())`。

---

## 5. 构造期控件就会发 `valueChanged`

`setRange()` / `setValue()` / `setDecimals()` 都会触发 `valueChanged`，
此时界面还没搭完，回调里碰 `self.grid` / `self.funnel_label` 就是 `AttributeError`。

**修法**：加一个就绪标志，`build()` 末尾置位，回调开头判断。

```python
def _on_filter_changed(self):
    if not self._ready:
        return
```

比在每个回调里 `hasattr` 干净，也顺带修掉以后新加的同类问题。

---

## 一个反复出现的主题

上面五条里有三条的共同点是：**出错时没有异常**，只是"看起来不对"
（空表、少了一半、越来越慢）。所以改 UI 之后**要看渲染结果**，
不能只看"没报错"。
