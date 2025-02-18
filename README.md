python auto_commit.py -h
usage: 自动提交代码到远程仓库 [-h] [-r REPO] [-o ORIGIN] [-b BRANCH] [-t TIME] [-s SENDER_MAIL] [-p PWD] [-e RECEIVER_MAIL]
                   [-l LOG]

options:
  -h, --help            show this help message and exit
  -r REPO, --repo REPO  仓库路径
  -o ORIGIN, --origin ORIGIN
                        远程仓库标识
  -b BRANCH, --branch BRANCH
                        远程仓库分支
  -t TIME, --time TIME  定时提交时间
  -s SENDER_MAIL, --sender_mail SENDER_MAIL
                        发件人邮箱
  -p PWD, --pwd PWD     发件人邮箱授权码
  -e RECEIVER_MAIL, --receiver_mail RECEIVER_MAIL
                        收件人邮箱
  -l LOG, --log LOG     日志文件