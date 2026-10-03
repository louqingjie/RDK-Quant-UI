# Qt 陷阱

改 `src/rdkquant/ui/` 之前扫一眼。**每条都真实踩过**，写的是"已经发生"。
根文件 [`AGENTS.md`](../AGENTS.md) 只列名字，这里放机制和怎么发现。

---

## 1. `processEvents()` 不处理 `DeferredDelete`

`deleteLater()` 只是把删除**排队**，而普通的 `processEvents()` **不处理
`DeferredDelete` 事件**——那个队列永远不会被执行。

**症状**：测试里控件越积越多。`app.topLevelWidgets()` 一轮涨 4 个、从不回落。
后果不只是内存——每次 `apply_theme()` 里的 `setStyleSheet()` 都要重新 polish
**所有**顶层控件，于是耗时随用例数线性增长：

```
第 1 轮  apply_theme    2.2 ms   顶层控件 4
第 4 轮  apply_theme 1182.5 ms   顶层控件 16      ← 增长 589 倍
```

**修法**（Qt6 里 `ProcessEventsFlag.DeferredDeletion` 已经移除）—— 加上下面那句之后，
判据「`apply_theme` 耗时是否平稳」从 **589x 增长**变成 **0.0x**
（`shiboken6.delete()` 立即销毁是 1.1x）：

```python
window.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
qapp.processEvents()
```

**怎么发现的**：测试套件慢到 238 秒，`--durations` 显示每个 UI 用例的 setup 要
8~12 秒。先怀疑图标缓存（量出来 0.2 ms，不贵），再量"界面到底有没有被销毁"，
才发现根本没销毁。修完全量 238 → 16 秒。

---

## 2. `QListView` 在 IconMode 下只读模型的第 0 列

用 `setItem(row, col)` 铺多列，**只有每行第一个会被画出来**，不报错。

**症状**：50 张缩略图只显示一排 9 个 —— `50 ÷ 6 列向上取整 = 9`，正是每行第一个。

**修法**：全部 `appendRow` 到第 0 列，换行交给视图按宽度自己流式排。

高度也得自己算（视图给不出合适的 `sizeHint`），**并且要有上限** ——
不然条目一多，外层滚动区会被撑到几万像素，把上面的控件顶走：

```python
per_row = max(1, self.viewport().width() // self.gridSize().width())
rows = min(GRID_ROWS, max(1, -(-count // per_row)))     # 上限 4 行
self.setFixedHeight(rows * gridSize.height() + 12)      # 超了由网格自己滚
```

`resizeEvent` 里要重算。

---

## 3. `make_table(headers)` 收的是表头列表，不是 model

传错**不会抛异常**，只会得到一张**全 0 宽的空表**。原因链：model 没设上 →
`columnCount() == 0` → `fit_columns_to_content` 提前返回 → 所有列宽为 0 → 什么都看不见。

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

**修法**：加一个就绪标志，`build()` 末尾置位，回调开头判断
（比在每个回调里 `hasattr` 干净，也顺带修掉以后新加的同类问题）：

```python
def _on_filter_changed(self):
    if not self._ready:
        return
```

---

## 6. 跨线程：`dict` 发不出去，`QPixmap` 造不出来

**两个坑，症状一样：没有异常，只是图一直不出来。**

**(a) `Signal` 的参数类型不能写 `dict`。** 编译得过，但跨线程排队投递时
PySide6 转不了：`Shiboken::Conversions::_pythonToCppCopy: Cannot copy-convert
...(dict) to C++`，结果**静默丢失**。写 `object`。

**(b) `QPixmap` / `QIcon` 只能在 GUI 线程创建。** 工作线程只产出 `QImage`
（可重入），主线程再转 `QIcon`。`ui/calibimages.py:load_thumbnail()` 因此
**只返回 `QImage`**，由 `test_calibimages.py` 守着。

配套两条：信号连**绑定方法**不连闭包（闭包被判直连，处理器跑在工作线程里）；
`QRunnable` 持有信号对象强引用，否则飞行中被 GC。

**怎么发现的**：`ui/widgets/thumbnailgrid.py` 做完按需解码后，"结果到达后图标
到位"这条断言一直失败，stderr 里才有那行 Shiboken 警告。

---

## 一个反复出现的主题

上面六条里有四条的共同点是：**出错时没有异常**，只是"看起来不对"
（空表、少了一半、图不出来、越来越慢）。所以改 UI 之后**要看渲染结果**，
不能只看"没报错"。
