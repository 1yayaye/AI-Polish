# word_formatter 作为库整体移植，不重写

推倒重写针对 Workspace 流水线与管理层（重灾区），但 word_formatter 子系统（约 7.3k 行，Markdown→AST→StyleSpec→OOXML 渲染管线）模块化良好且已工作；其依赖链 python-docx / lxml / mistune 是 Python 原生的，没有跨语言同级别替代，重写只有风险没有收益。决定：将其当作库整体移植进新骨架，仅调整对外接口（任务管理、认证、配置）适配新架构，内部代码原样迁移。注意：换栈到 Go/Node 曾因此被否决。
