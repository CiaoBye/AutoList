# 问题跟踪方式：本地 Markdown

本仓库的需求规格与实现票据保存在 `.scratch/`。

## 文件约定

- 每个功能使用一个目录：`.scratch/<feature-slug>/`
- 规格文件：`.scratch/<feature-slug>/spec.md`
- 实现票据：`.scratch/<feature-slug>/issues/<NN>-<slug>.md`
- 每张票据顶部保留 `Status:` 状态行；技能要求的机器状态值保持原样。
- 讨论记录追加在 `## Comments` 下。

## 发布约定

当工程技能要求“发布到问题跟踪器”时，在对应功能目录下创建或更新 Markdown 文件，不调用外部服务。
