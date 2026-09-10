# 自动提交代码到远程仓库

`auto_commit.py` 是一个用于自动提交代码到远程仓库的脚本，并在提交成功或失败时发送邮件通知。该工具特别适用于需要定期备份代码、自动化部署流程或保持仓库活跃度的场景。

## 功能特性

- ✅ 定时自动提交代码变更
- ✅ 支持多个时间点定时执行
- ✅ 邮件通知提交结果（成功/失败）
- ✅ 详细的日志记录


## 安装要求

### 系统要求
- Python 3
- Git 2.0+
- 网络连接（用于推送到远程仓库和发送邮件）

### Python依赖
```bash
pip install schedule
```

### Git配置
确保本地Git已配置用户信息和远程仓库访问权限：
```bash
git config --global user.name "Your Name"
git config --global user.email "your.email@example.com"
```

## 使用方法
```bash
usage: auto_commit.py [-h] -r REPO -o REMOTE -b BRANCH [-t TIME] [-e RECEIVER_MAIL] [-l LOG]

自动定时提交代码到远程仓库并发送提示邮件

options:
  -h, --help            show this help message and exit
  -r REPO, --repo REPO  本地git仓库路径 (必须参数)
  -o REMOTE, --remote REMOTE  远程仓库标识 (必须参数)
  -b BRANCH, --branch BRANCH  远程仓库分支 (必须参数)
  -t, --time TIME       每日定时提交时间, 格式：HH:MM，多个时间用逗号分隔 (必须参数, default: 23:30)
  -e RECEIVER_MAIL, --receiver_mail RECEIVER_MAIL   收件人邮箱，不指定该参数则不发送邮件 (可选参数)
  -l LOG, --log LOG     日志文件 (必须参数, default: auto_commit.log)
```

### 参数说明

| 参数 | 简写 | 类型 | 必需 | 默认值 | 说明 |
|------|------|------|------|--------|------|
| `--repo` | `-r` | string | ✅ | - | 本地Git仓库的绝对路径 |
| `--remote` | `-o` | string | ✅ | - | 远程仓库名称（通常是origin） |
| `--branch` | `-b` | string | ✅ | - | 目标分支名称 |
| `--time` | `-t` | string | ❌ | 23:30 | 定时执行时间，格式HH:MM，多个时间用逗号分隔 |
| `--receiver_mail` | `-e` | string | ❌ | - | 接收通知的邮箱地址 |
| `--log` | `-l` | string | ❌ | auto_commit.log | 日志文件路径 |


## 使用示例

### 基础使用
```bash
# 每天23:30自动提交到main分支
python auto_commit.py -r /home/user/my_project -o origin -b main
```

### 多时间点提交
```bash
# 每天09:00和18:00自动提交
python auto_commit.py -r /home/user/my_project -o origin -b main -t 09:00,18:00
```

### 带邮件通知
```bash
# 提交并发送邮件通知
python auto_commit.py -r /home/user/my_project -o origin -b main -t 23:30 -e admin@company.com
```

### 自定义日志文件
```bash
# 指定日志文件位置
python auto_commit.py -r /home/user/my_project -o origin -b main -l /var/log/auto_commit.log
```

### 后台运行（推荐）
```bash
# 使用nohup在后台运行
nohup python auto_commit.py -r /home/user/my_project -o origin -b main -t 23:30 -e admin@company.com > /dev/null 2>&1 &

# 或使用screen/tmux
screen -S auto_commit
python auto_commit.py -r /home/user/my_project -o origin -b main -t 23:30 -e admin@company.com
# Ctrl+A+D 分离会话
```


## 工作流程

1. **初始化**: 检查Git仓库状态和远程连接
2. **定时检查**: 按设定时间检查是否有代码变更
3. **自动提交**: 发现变更时自动add、commit和push
4. **通知发送**: 根据操作结果发送邮件通知
5. **日志记录**: 记录所有操作和错误信息

## 故障排除

### 常见问题

**Q: 脚本运行后没有反应？**
A: 检查当前时间是否已过设定的提交时间，脚本会在下一个设定时间点执行。

**Q: 推送失败怎么办？**
A: 检查网络连接、Git权限配置和远程仓库状态。

**Q: 邮件发送失败？**
A: 确认SMTP配置正确，检查网络连接和邮箱服务器设置。

**Q: 如何停止自动提交？**
A: 找到进程ID并终止：
```bash
ps aux | grep auto_commit.py
kill <进程ID>
```

### 日志分析

日志文件包含以下信息：
- 脚本启动时间
- 每次检查的时间戳
- Git操作结果
- 邮件发送状态
- 错误详情和堆栈信息

## 最佳实践

1. **测试环境**: 首先在测试仓库上验证脚本功能
2. **权限管理**: 使用SSH密钥或Personal Access Token进行Git认证
3. **监控日志**: 定期检查日志文件，及时发现问题
4. **备份配置**: 保存脚本配置和重要设置
5. **网络稳定**: 确保服务器网络连接稳定


## 更新日志

- **v1.0.0**: 初始版本，支持基础的定时提交和邮件通知功能

---

如有问题或建议，请通过Issue联系我们。