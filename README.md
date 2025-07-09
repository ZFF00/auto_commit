# 自动提交代码到远程仓库

`auto_commit.py` 是一个用于自动提交代码到远程仓库的脚本，并在提交成功或失败时发送邮件通知。

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

## 示例
```bash
python auto_commit.py -r /path/to/repo -o origin -b main -t 23:30 -e receiver_email@example.com
# 建议在后台运行，即：
python auto_commit.py -r /path/to/repo -o origin -b main -t 23:30 -e receiver_email@example.com &
```