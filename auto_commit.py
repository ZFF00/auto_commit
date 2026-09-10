import os
import argparse
import subprocess
import re
import schedule
import time
import datetime
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
        formatter_class=CustomHelpFormatter
    )
    parser.add_argument('-r', '--repo', help='本地git仓库路径', required=True)
    parser.add_argument('-o', '--remote', help='远程仓库标识', required=True)
    parser.add_argument('-b', '--branch', help='远程仓库分支', required=True)
    parser.add_argument('-t', '--time', help='每日定时提交时间, 格式：HH:MM，多个时间用逗号分隔', default="23:30")
    parser.add_argument('-e', '--receiver_mail', help='收件人邮箱，不指定该参数则不发送邮件')
    parser.add_argument('-l', '--log', help='日志文件', default='auto_commit.log')
    args = parser.parse_args()
    if len(vars(args)) < 1:
        parser.print_help()
        exit(1)
    if not os.path.exists(args.repo):
        print(f"仓库路径不存在: {args.repo}")
        exit(1)
    time_points = [t.strip() for t in args.time.split(',')]
    time_pattern = re.compile(r'^([01]?\d|2[0-3]):([0-5]?\d)$')
    for time_point in time_points:
        if not time_pattern.match(time_point):
            print(f"时间格式错误: {time_point}，请使用HH:MM格式")
            exit(1)
    sender_mail = '1927466262@qq.com'
    pwd = 'oyznvtookrwybhgc'
    return args.repo, args.remote, args.branch, time_points, sender_mail, pwd, args.receiver_mail, args.log

# 获取服务器时区与UTC的偏移（单位：小时）
def get_timezone_offset():
    if hasattr(time, "localtime") and hasattr(time.localtime(), "tm_gmtoff"):
        offset_sec = time.localtime().tm_gmtoff
        return offset_sec // 3600
    try:
        import subprocess
        out = subprocess.check_output("date +%z", shell=True).decode().strip()
        sign = 1 if out[0] == '+' else -1
        hours = int(out[1:3])
        return sign * hours
    except Exception:
        return 0

# 将北京时间点转换为服务器本地时间点（字符串列表）
def convert_beijing_time_to_local(beijing_times):
    beijing_offset = 8
    local_offset = get_timezone_offset()
    delta = local_offset - beijing_offset
    local_times = []
    for t in beijing_times:
        h, m = map(int, t.split(':'))
        dt = datetime.datetime(2000, 1, 1, h, m) + datetime.timedelta(hours=delta)
        local_times.append(dt.strftime('%H:%M'))
    return local_times, delta

# 检查是否全部文件都已经提交
# git version 1.8.3.1
def get_git_status(repo_path):
    """获取git仓库状态"""
    os.chdir(repo_path)
    result, error, returncode = subprocess_popen("git status")
    return result

def all_files_commited(repo_path):
    """检查是否有未提交的文件"""
    status = get_git_status(repo_path)
    status = '\n'.join(status)
    return "nothing to commit" in status

def send_unchanged_mail(repo_name, status_info, sender_mail, pwd, receiver_mail):
    """发送未改动邮件"""
    status_info = '<br>'.join(status_info)
    mail_info = {
        "sender_mail": sender_mail,
        "pwd": pwd,
        "receiver_mail": receiver_mail,
        "mail_title": f"✅git仓库今日无改动【{repo_name}】",
        "mail_content": f"<b style='color:green'>【git status】：</b><font style='color:black'><br/>{status_info}</font><br/>"
    }
    send_mail(mail_info)

def send_commit_mail(repo_name, commit_infos, sender_mail, pwd, receiver_mail):
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
        mail_info['mail_title'] = f'❌️git仓库提交失败【{repo_name}】' 
    else:
        mail_info['mail_title'] = f'✅git仓库提交成功【{repo_name}】'
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

def auto_commit(repo_path, remote, branch, repo_name, sender_mail, pwd, receiver_mail, log_file):
    print(' 时间到 ，开始自动提交代码...')
    """自动提交代码到远程仓库"""
    # 检查是否有未提交的文件
    if all_files_commited(repo_path):
        status_info = get_git_status(repo_path)
        if receiver_mail:
            send_unchanged_mail(repo_name, status_info, sender_mail, pwd, receiver_mail)
        commit_status = -1
        log_info = status_info
    else:
        # 提交代码
        commit_infos = commit(repo_path, remote, branch)
        if receiver_mail:
            send_commit_mail(repo_name, commit_infos, sender_mail, pwd, receiver_mail)
        commit_status = commit_infos[-1][-1]
        log_info = commit_infos
    # 写日志
    write_log(commit_status, log_file, log_info)

def get_remote_info(repo_path, remote_name):
    """获取远程仓库信息"""
    os.chdir(repo_path)
    
    # 获取远程仓库URL
    result, error, returncode = subprocess_popen(f"git remote get-url {remote_name}")
    if returncode != 0:
        return None, None

    remote_url = result[0]
    repo_name = remote_url.replace('git@github.com:', '').replace('.git', '')
    return remote_url, repo_name

def main():
    repo_path, remote, branch, beijing_times, sender_mail, pwd, receiver_mail, log_file = get_arguments()
    remote_url, repo_name = get_remote_info(repo_path, remote)
    if not remote_url:
        print(f"远程仓库标识 '{remote}' 未对应到任何远程仓库，请检查仓库路径和远程标识是否正确")
        exit(1)
    print(f"仓库路径: {repo_path}\n远程仓库: {remote_url}")
    
    local_times, delta = convert_beijing_time_to_local(beijing_times)
    if delta == 0:
        print("服务器时区为北京时间，无需转换。")
    else:
        print(f"服务器时区与北京时间相差 {delta} 小时。")
        print(f"你输入的北京时间点: {', '.join(beijing_times)}")
        print(f"将在服务器本地时间点: {', '.join(local_times)} 执行定时任务。")

    with open(log_file, 'w') as f:
        f.write(f"########################### 自动提交日志 ###########################\n")
        f.write(f"### 本地仓库: {repo_path}\n")
        f.write(f"### 远程仓库: {remote_url}\n")
        f.write(f"### 提交时间: {', '.join(beijing_times)} (服务器本地时间: {', '.join(local_times)})\n")
        f.write(f"###################################################################\n\n")
    for time_point in local_times:
        schedule.every().day.at(time_point).do(auto_commit, repo_path, remote, branch, repo_name, sender_mail, pwd, receiver_mail, log_file)
        print(f"已设置定时任务: 每天 {time_point} (服务器本地时间) 自动提交")
    print(f"共设置 {len(local_times)} 个定时任务")
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()