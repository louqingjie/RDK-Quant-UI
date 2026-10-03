# 提交信息格式

[根 AGENTS.md](../AGENTS.md) 的「约束一」要求自动提交，这里放模板。**写提交信息时看这份。**

## 格式

```
<一句话说清修了什么>

<现象／用户报告的原话>

## 根因
<为什么坏。要具体到机制，不要写"逻辑错误">

## 取舍
问题：
选择：
替代方案与代价：

## 验证
<实测数字。命令 + 结果>
```

四段不是仪式。「根因」逼你说清机制而不是症状；「取舍」是[约束三](../AGENTS.md)要求的记录；
「验证」让"测过了"和"应该可以"没法混在一起。

修理文档漂移时，在「根因」那段写清**哪一处漂移、代码和文档各说了什么、为什么代码是对的**。

## 命令

```powershell
git add -A
git -c core.autocrlf=false commit -q -F "$env:TEMP\msg.txt"
Remove-Item "$env:TEMP\msg.txt" -Force
git status --short          # 应当无输出
```

先把信息写进临时文件（用编辑工具写，保证 UTF-8），提交完删掉。

## 两个必须记住的开关

**`-F` 传文件，不能用 `-m`。** 多行中文信息用 `-m` 会被 PowerShell 拆散——
`——` 之类的字符会触发参数分割，实测报：

```
error: pathspec '缩放，但 calib-build 早就把任意尺寸的图缩放到 --shape 了 ——
缺的只是挑哪几张。所以核心是采样：' did not match any file(s) known to git
```

提交没成功，而且看起来像 git 的错，不像 shell 的错。

**`core.autocrlf=false` 必须带。** 仓库里有 `.cmd` 和 shell 脚本，换行符被 git 改写会坏
（`scripts/run-windows.cmd` 就因为换行/编码问题出过事，`test_repo_hygiene.py` 守着这条）。

## 一个提交只做一件事

不要把"修 bug"和"顺手重构"混在一个提交里——回滚时没法只回滚一半。
发现不相关的坏味道，单独开一个提交。
