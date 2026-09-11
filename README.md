# Codex Git 提交建议工具

`auto_commit.py` 会检查 Git 工作区的暂存、未暂存和未跟踪文件，调用本机安装的 Codex CLI，并输出：

- 不建议提交的文件及建议的 `.gitignore` 规则；
- 可直接使用的 commit 标题与正文；
- 建议包含、排除或拆分处理的文件。

脚本只做分析，不会执行 `git add`、`git commit`、`git push`，也不会修改 `.gitignore`。

## 环境要求

- Python 3.10+
- Git
- 已安装并登录 Codex CLI

确认 Codex 可用：

```bash
codex --version
codex login
```

脚本仅使用 Python 标准库，无需安装额外依赖。

## 使用方法

在需要分析的 Git 仓库中运行：

```bash
python /path/to/auto_commit.py
```

也可以显式指定仓库：

```bash
python auto_commit.py --repo /path/to/repository
```

常用选项：

```text
-r, --repo PATH             Git 仓库内的路径
-m, --model MODEL           指定 Codex 模型，默认使用本机配置
--json                      输出结构化 JSON
--no-untracked-content      只提供未跟踪文件名，不提供文件内容
--max-input-chars NUMBER    限制传给 Codex 的差异大小
--timeout SECONDS           Codex 执行超时
```

查看完整帮助：

```bash
python auto_commit.py --help
```

## 输出示例

```text
仓库：D:\work\demo
分支：main
摘要：新增用户查询接口，并包含本地缓存文件。

不建议提交 / 建议加入 .gitignore：
1. __pycache__/app.cpython-312.pyc（置信度：high）
   原因：Python 解释器生成的缓存文件。
   建议规则：__pycache__/

建议 commit 信息：
feat: add user query endpoint
```

## 数据与安全

脚本在本地先收集 Git 状态和差异，再把整理后的文本交给 `codex exec`。它会：

- 对 `.env`、私钥、凭据文件等疑似敏感文件只传递路径，不读取内容；
- 隐藏普通差异中常见的密码、令牌和 API Key 赋值行；
- 跳过二进制文件并限制单文件与总体输入大小；
- 用只读沙箱和临时会话调用 Codex。

启用 `--no-untracked-content` 可以进一步减少发送给 Codex 的内容，但新文件对应的 commit 建议会不够具体。Codex CLI 的实际数据处理方式取决于本机配置与所用服务。

## 退出码

- `0`：分析成功，或工作区没有更新；
- `2`：参数、Git 或 Codex 执行错误；
- `130`：用户中断。
