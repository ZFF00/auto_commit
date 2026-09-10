# Codex Git 自动推送工具

这是一个面向 Git 仓库完整留存场景的自动提交与推送工具。它由三个职责独立的模块组成：

当前版本：`3.0.0`

- `codex_git_advisor.py`：让本机 Codex 以只读方式检查仓库，给出应包含、应忽略、需人工确认的路径，以及 `.gitignore` 规则和 commit message；
- `auto_commit.py`：执行单次任务或定时调度，安全地更新 `.gitignore`、暂存批准的文件、提交、推送并发送邮件通知；
- `email_notifier.py`：生成纯文本与 HTML 邮件并通过 SMTP 发送。

## 主要行为

- 以完整备份为目标，源码、测试、文档、共享配置和有价值的旧版本默认提交；
- 只自动忽略 Codex 判定为 `high` 置信度的缓存、日志、生成文件等内容；
- 用 Git 实际验证忽略规则，规则未覆盖目标文件或会误伤应提交文件时停止；
- Codex 必须为每一个当前变更路径分类，漏项、多项重叠或返回仓库外路径时停止；
- 有大文件用途不明、疑似真实凭据或其他阻断风险时停止自动提交；
- `--live` 会对常见密钥、Token 和密码赋值进行脱敏后再显示；
- 只暂存 Codex 批准的路径，不使用笼统的 `git add .`；
- 使用仓库内锁文件防止两个任务同时运行；
- 推送失败时保留已经完成的本地提交；下一次任务即使没有新改动，也会跳过 Codex 并直接再次推送。
- 邮件同时包含纯文本和 HTML 版本，并按成功、无变化、演练或失败显示不同状态。

## 环境要求

- Python 3.10+
- Git
- 已安装并登录 Codex CLI
- Git 仓库已配置提交用户名、邮箱和远程仓库

检查环境：

```powershell
codex --version
codex login
git config user.name
git config user.email
git remote -v
```

如果当前仓库还没有远程仓库，可配置为：

```powershell
git remote add origin git@github.com:OWNER/REPOSITORY.git
```

脚本只使用 Python 标准库，不需要额外安装 Python 包。在 Windows 上会自动查找 PATH、npm 全局目录和 Codex Desktop 中的 `codex.exe`；仍无法定位时可以通过 `--codex` 指定完整路径。

## 可执行文件

项目为 64 位 Windows 和 64 位 Linux 构建单文件程序：

- `auto_commit-windows-x86_64.exe`
- `auto_commit-linux-x86_64`

可执行文件无需安装 Python，但仍需要系统提供 Git 和 Codex CLI。Windows 版本可直接在命令提示符或 PowerShell 中运行：

```powershell
.\auto_commit-windows-x86_64.exe --once --live
```

Linux 版本首次运行前需要增加执行权限：

```bash
chmod +x auto_commit-linux-x86_64
./auto_commit-linux-x86_64 --once --live
```

每次向 `main` 推送 Python 源码时，GitHub Actions 都会重新构建并验证两个平台的程序。构建产物保留 30 天，可在仓库的 Actions 页面下载。它们属于可重新生成的发布产物，不提交到 Git。

## 首次演练

建议先执行只读演练。它会真实调用 Codex 并验证结果，但不会修改 `.gitignore`、暂存、提交或推送：

```powershell
python auto_commit.py --once --dry-run --live
```

如果输出含有“需人工确认”或“阻断自动提交”，先处理对应问题再正式运行。

## 单次提交并推送

使用当前目录、`origin` 远程仓库和当前分支：

```powershell
python auto_commit.py --once --live
```

指定仓库、远程仓库和目标分支：

```powershell
python auto_commit.py --once --repo D:\work\project --remote origin --branch main --live
```

只创建本地提交、不推送：

```powershell
python auto_commit.py --once --no-push
```

## 邮件通知

发件邮箱、SMTP 授权码和收件邮箱既可以在本次命令中指定，也可以通过环境变量提供。命令行显式指定的值优先，未指定的项再逐项读取环境变量。

直接指定本次运行使用的值：

```powershell
python auto_commit.py --once --mail-sender "sender@qq.com" --mail-auth-code "SMTP授权码" --mail-recipients "receiver@example.com"
```

授权码出现在命令行时可能被终端历史或进程查看工具记录，因此长期定时任务更适合使用环境变量：

```powershell
$env:MAIL_SENDER = "sender@qq.com"
$env:MAIL_AUTH_CODE = "在邮箱后台生成的SMTP授权码"
$env:MAIL_RECIPIENTS = "receiver@example.com"
python auto_commit.py --once --live
```

多个收件邮箱可以用逗号或分号分隔：

```powershell
$env:MAIL_RECIPIENTS = "first@example.com,second@example.com"
```

上述写法只对当前 PowerShell 窗口有效。要保存为 Windows 当前用户的持久环境变量：

```powershell
[Environment]::SetEnvironmentVariable("MAIL_SENDER", "sender@qq.com", "User")
[Environment]::SetEnvironmentVariable("MAIL_AUTH_CODE", "在邮箱后台生成的SMTP授权码", "User")
[Environment]::SetEnvironmentVariable("MAIL_RECIPIENTS", "receiver@example.com", "User")
```

设置后需要重新打开终端或重新启动定时任务。不要把真实授权码写进 `.env`、脚本、README 或命令行。

QQ、163、126、Gmail、Outlook、Hotmail 和 Live 邮箱会自动推断 SMTP 服务器、端口和安全模式。其他邮箱还需设置：

```powershell
$env:MAIL_SMTP_HOST = "smtp.example.com"
$env:MAIL_SMTP_PORT = "465"
$env:MAIL_SMTP_SECURITY = "ssl"
```

邮件固定为所有结果都通知，发件人显示名称为“Codex Git 推送”，SMTP 超时为 30 秒。三项可以混合配置，例如只传 `--mail-recipients` 时，发件邮箱和授权码仍从环境变量读取。只要最终配置完整，邮件会自动启用。`--email` 可要求邮件必须启用并在缺少配置时立即报错；`--no-email` 可临时关闭。邮件标题使用“Git 自动推送”和远程地址中的 `用户/仓库`（例如 `ZFF00/auto_commit`），正文分别展示本地仓库完整路径和 Git 配置中保存的原始远程仓库地址（例如 `git@github.com:ZFF00/auto_commit.git`），并包含分支、时间、commit、推送状态、新增忽略规则和任务报告。

## 内置定时任务

不使用 `--once` 时，脚本进入常驻定时模式。默认每天 `23:30` 执行：

```powershell
python auto_commit.py --live
```

每天 `09:00` 和 `18:00` 执行，并在启动时先执行一次：

```powershell
python auto_commit.py --time 09:00 --time 18:00 --run-now --live --log-file auto_commit.log
```

时间也可以写成一个逗号分隔参数：

```powershell
python auto_commit.py --time 09:00,18:00
```

常驻模式需要终端或后台进程持续运行。也可以使用 Windows 任务计划程序定时调用 `python auto_commit.py --once`，此时调度由 Windows 管理，脚本仍负责完整的单次分析、提交和推送流程。

## 独立查看 Codex 建议

只需要分析报告、不执行任何 Git 写操作时，直接运行：

```powershell
python codex_git_advisor.py --live
```

结构化输出：

```powershell
python codex_git_advisor.py --json
```

## 单次任务流程

1. 确认目标是 Git 仓库，并解析当前分支。
2. 正式模式检查 Git 提交身份和远程仓库是否存在。
3. 获取仓库锁，防止任务重叠。
4. 如果工作区干净，跳过 Codex 并直接尝试推送当前 `HEAD`。
5. 如果存在改动，调用 `codex_git_advisor.py`，由 Codex 在仓库目录中执行只读检查。
6. 验证每个变更文件恰好属于“包含、排除、人工确认”之一。
7. 验证忽略建议为高置信度、目标未被跟踪、规则足够精确且不会误伤包含路径。
8. 更新 `.gitignore`，只按字面路径暂存建议包含的文件和本次 `.gitignore` 更新，然后创建本地 commit。
9. 将当前 `HEAD` 推送到指定远程分支。

脚本不会执行 `git pull`、`git reset`、`git clean`、`git rm --cached` 或强制推送。

## 备份优先的分类规则

- `README.md`、`auto_commit.py`、测试文件和其他有效源码、文档：默认包含；
- `__pycache__/`、`nohup.out`：通常排除；
- 无扩展名的 `auto_commit`：确认是可重新生成的 ELF/打包二进制后排除；
- 明确不需要备份的大型生成数据：排除；
- 用途、来源或可重新生成性不明确的大文件：人工确认，不会只因体积大就忽略；
- 含疑似真实密钥、Token 或密码的有效源码：不忽略整个源码，但阻断自动提交，必须先移除并轮换凭据。

文件未跟踪、属于旧版本、与其他文件主题不同、测试失败或建议拆分 commit，都不能作为忽略理由。一般代码质量提醒不会阻止备份，只有泄密、数据损失或错误发布等风险会阻断。

## 失败后的状态

- Codex 调用失败、分类不完整、存在人工确认项或阻断风险：不会修改仓库；
- 远程仓库或 Git 身份未配置：在调用 Codex 和修改仓库前失败；
- 本地 commit 成功但 push 失败：本地 commit 会保留，下次任务会再次推送；
- 写入 `.gitignore` 或暂存后 Git commit 失败：修改和暂存状态会保留，便于人工检查，不会执行自动回滚；
- 任务异常退出后如果留下 `.git/auto_commit.lock`，确认没有其他任务运行后再人工删除该锁文件。

## 常用参数

```text
-r, --repo PATH             Git 仓库路径，默认当前目录
--once                      立即执行一次后退出
-t, --time HH:MM            每日时间，可重复或逗号分隔
--run-now                   启动常驻定时器时先执行一次
--remote NAME               远程仓库名，默认 origin
--branch NAME               远程分支，默认当前分支
--no-push                   只提交到本地
--dry-run                   只分析和验证
--live                      实时显示 Codex 执行过程
--timeout SECONDS           Codex 超时，默认 300 秒
--codex PATH                Codex CLI 命令或完整路径
--log-file PATH             同时记录运行日志
--email                     要求启用邮件，最终配置不完整时立即报错
--no-email                  本次运行临时关闭邮件
--mail-sender ADDRESS       发件邮箱，优先于 MAIL_SENDER
--mail-auth-code CODE       SMTP 授权码，优先于 MAIL_AUTH_CODE
--mail-recipients ADDRESSES 收件邮箱，优先于 MAIL_RECIPIENTS
```

查看完整帮助：

```powershell
python auto_commit.py --help
python codex_git_advisor.py --help
```

## 测试

```powershell
python -B -m unittest discover -v
python -m py_compile auto_commit.py codex_git_advisor.py email_notifier.py test_auto_commit.py test_codex_git_advisor.py test_email_notifier.py
```

测试使用临时工作仓库和本地裸远程仓库验证真实的 `.gitignore`、commit 与 push，不会访问项目配置的 GitHub 远程仓库。

## 数据与安全边界

Python 脚本不会把预先拼接的仓库快照放入提示词。Codex 直接在目标仓库中使用只读命令检查 Git 状态、差异、文件大小和必要内容；大文件只应抽样读取，疑似敏感文件只根据路径和元数据判断。

Codex 使用 `read-only` 沙箱和临时会话运行，并关闭插件、远程插件、应用、浏览器、计算机控制、多代理和 hooks。大文件抽样及敏感内容限制属于交给 Codex 的任务约束，并不是 Python 层面的文件访问隔离。极高敏感仓库不应直接交给远程模型检查。
