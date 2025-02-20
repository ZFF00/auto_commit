import os
import argparse
import subprocess
import schedule
import time
from email.mime.text import MIMEText
from email.header import Header
from smtplib import SMTP_SSL, SMTP

def send_mail(mail_info):
    """

    Parameters
    ----------
    mail_info : dict
        sender_mail : str
            发件人邮箱.
        pwd : str
            邮箱授权码.
        receiver_mail : str
            收件人邮箱.
        mail_title : str
            邮件标题.
        mail_content : str
            邮件正文内容.

    Returns
    -------
    None.

    """
    mail = ["sender_mail", "pwd", "receiver_mail",
            "mail_title", "mail_content"]
    for key in mail:
        if key in mail_info:
            pass
        else:
            raise Exception("需要参数%s" % key)

    # 解析参数
    sender_mail = mail_info["sender_mail"]
    pwd = mail_info["pwd"]
    receiver_mail = mail_info["receiver_mail"]
    mail_title = mail_info["mail_title"]
    mail_content = mail_info["mail_content"]

    # qq邮箱smtp服务器
    host_server = 'smtp.qq.com'

    smtp = SMTP_SSL(host_server)
    # set_debuglevel()是用来调试的。参数值为1表示开启调试模式，参数值为0关闭调试模式
    smtp.set_debuglevel(0)
    smtp.ehlo(host_server)

    smtp.login(sender_mail, pwd)

    msg = MIMEText(mail_content, "html", 'utf-8')
    msg["Subject"] = Header(mail_title, 'utf-8')
    msg["From"] = Header('邮箱助手', 'utf-8')
    msg["From"].append(f"<{sender_mail}>", 'ascii')
    msg["To"] = receiver_mail
    smtp.sendmail(sender_mail, receiver_mail, msg.as_string())
    smtp.quit()


def subprocess_popen(command, work_dir=None, se_PIPE=True, so_PIPE=True):
    # 执行系统命令
    import os
    import re
    import subprocess
    code = 'gbk' if os.name == 'nt' else 'utf-8'
    so = subprocess.PIPE if so_PIPE else None   # 指定标准输出到哪
    se = subprocess.PIPE if se_PIPE else None   # 指定标准错误输出到哪
    p = subprocess.Popen(command, shell=True, stdout=so,
                         stderr=se, cwd=work_dir)
    data, error = p.communicate()    # communicate()等待子进程结束，从stdout和stderr读数据返回元组
    data, error = ('' if i is None else i.decode(code) for i in (data, error))
    result = re.split(r'[\r\n]+', data.strip('\r\n'))
    error_info = re.split(r'[\r\n]+', error.strip('\r\n'))
    return result, error_info, p.returncode

class CustomHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    def _get_help_string(self, action):
        help = action.help
        if action.option_strings == ['-h', '--help']:
            return help
        help_default = ''
        if action.default is not argparse.SUPPRESS and action.default is not None:
            help_default = 'default: %(default)s'
            action.required = True
        help_required = '必须参数' if action.required else '可选参数'
        if help_default:
            help += f' ({help_required}, {help_default})'
        else:
            help += f' ({help_required})'
        return help


def get_arguments():
    parser = argparse.ArgumentParser(
        description='自动定时提交代码到远程仓库并发送提示邮件',
        # formatter_class=argparse.ArgumentDefaultsHelpFormatter
        formatter_class=CustomHelpFormatter
    )
    # 仓库路径
    parser.add_argument('-r', '--repo', help='本地git仓库路径', required=True)
    # 远程仓库标识
    parser.add_argument('-o', '--remote', help='远程仓库标识', required=True)
    # 远程仓库分支
    parser.add_argument('-b', '--branch', help='远程仓库分支', required=True)
    # 定时提交时间
    parser.add_argument('-t', '--time', help='每日定时提交时间, 格式：HH:MM', default="23:30")
    # # 发件人邮箱
    # parser.add_argument('-s', '--sender_mail', help='发件人邮箱', default='1927466262@qq.com')
    # # 发件人邮箱授权码
    # parser.add_argument('-p', '--pwd', help='发件人邮箱授权码', default='oyznvtookrwybhgc')
    # 收件人邮箱
    parser.add_argument('-e', '--receiver_mail', help='收件人邮箱，不指定该参数则不发送邮件')
    # 日志文件
    parser.add_argument('-l', '--log', help='日志文件', default='auto_commit.log')
    args = parser.parse_args()
    # 参数长度小于1时，输出帮助信息
    if len(vars(args)) < 1:
        parser.print_help()
        exit(1)
    if not os.path.exists(args.repo):
        print(f"仓库路径不存在: {args.repo}")
        exit(1)
    sender_mail = '1927466262@qq.com'
    pwd = 'oyznvtookrwybhgc'
    return args.repo, args.remote, args.branch, args.time, sender_mail, pwd, args.receiver_mail, args.log
    # return args.repo, args.remote, args.branch, args.time, args.sender_mail, args.pwd, args.receiver_mail, args.log


# 检查是否全部文件都已经提交
# git version 1.8.3.1
def get_git_status(repo_path):
    """获取git仓库状态"""
    os.chdir(repo_path)
    result, error, returncode = subprocess_popen("git status")
    return result

def all_files_committed(repo_path):
    """检查是否有未提交的文件"""
    status = get_git_status(repo_path)
    status = '\n'.join(status)
    return "nothing to commit" in status

def send_unchanged_mail(status_info, sender_mail, pwd, receiver_mail):
    """发送未改动邮件"""
    status_info = '<br>'.join(status_info)
    mail_info = {
        "sender_mail": sender_mail,
        "pwd": pwd,
        "receiver_mail": receiver_mail,
        "mail_title": "✅git仓库今日无改动",
        "mail_content": f"<b style='color:green'>【git status】：</b><font style='color:black'><br/>{status_info}</font><br/>"
    }
    send_mail(mail_info)


def send_commit_mail(commit_infos, sender_mail, pwd, receiver_mail):
    """发送命令执行结果"""
    mail_info = {
        "sender_mail": sender_mail,
        "pwd": pwd,
        "receiver_mail": receiver_mail,
        "mail_title": "命令执行结果",
        "mail_content": ""
    }
    for command, success, error, returncode in commit_infos:
        success_info = '<br/>'.join(success)
        error_info = '<br/>'.join(error)
        mail_info['mail_content'] += f"<b style='color:green'>【{command}】：</b><br/>"
        if error_info:
            mail_info['mail_content'] += f"<font style='color:black'>{error_info}</font><br/><br/>"
        if success_info:
            mail_info['mail_content'] += f"<font style='color:black'>{success_info}</font><br/><br/>"
        if not success_info and not error_info:
            mail_info['mail_content'] += f"<br/><br/>"
    
    if returncode:
        mail_info['mail_title'] = '❌️git仓库提交失败' 
    else:
        mail_info['mail_title'] = '✅git仓库提交成功'
    send_mail(mail_info)
    return returncode

def write_log(commit_status, log_file, log_info):
    # commit: -1未提交，0提交成功，>1提交失败
    # 如果未提交，log_info为git status，否则为命令执行结果
    content = f"【{time.strftime('%Y-%m-%d %H:%M:%S')}】 "
    if commit_status == -1:
        content += f"（无改动）：\n"

    elif commit_status == 0:
        content += f"（提交成功）：\n"
    else:
        content += f"（提交失败）：\n"
    """写日志"""
    flog = open(log_file, 'a')
    if commit_status == -1:
        content += '-' * 30 + 'git status' + '-' * 30 + '\n'
        content += '\n'.join(log_info)
    else:
        for command, success, error, returncode in log_info:
            content += f"{'-' * 30} {command} {'-' * 30}\n"
            content += '\n'.join(success) + '\n'
            content += '\n'.join(error) + '\n'
    content += '\n\n\n'
    flog.write(content)
    flog.close()

def commit(repo_path, remote, branch):
    """自动提交代码到远程仓库"""
    """运行命令并返回输出"""
    # 切换到 Git 仓库目录
    os.chdir(repo_path)
    commands = [
        "git status",
        "git add -A",
        "git commit -m 'daily update'",
        f"git push {remote} {branch}"
    ]   
    command_infos = [(command, *subprocess_popen(command)) for command in commands]
    return command_infos

def auto_commit(repo_path, remote, branch, sender_mail, pwd, receiver_mail, log_file):
    """自动提交代码到远程仓库"""
    # 检查是否有未提交的文件
    if all_files_committed(repo_path):
        status_info = get_git_status(repo_path)
        if receiver_mail:
            send_unchanged_mail(status_info, sender_mail, pwd, receiver_mail)
        commit_status = -1
        log_info = status_info
    else:
        # 提交代码
        commit_infos = commit(repo_path, remote, branch)
        if receiver_mail:
            send_commit_mail(commit_infos, sender_mail, pwd, receiver_mail)
        commit_status = commit_infos[-1][-1]
        log_info = commit_infos
    # 写日志
    write_log(commit_status, log_file, log_info)

def main():
    repo_path, remote, branch, send_time, sender_mail, pwd, receiver_mail, log_file = get_arguments()
    schedule.every().day.at(send_time).do(auto_commit, repo_path, remote, branch, sender_mail, pwd, receiver_mail, log_file)
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()