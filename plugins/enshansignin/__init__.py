import re
import requests
from app.plugins import _PluginBase
from app.core.event import eventmanager, Event
from app.log import logger
from apscheduler.triggers.cron import CronTrigger

class Plugin(_PluginBase):
    # 插件元数据
    plugin_meta = {
        "id": "enshan_signin",
        "name": "恩山论坛签到",
        "version": "1.0",
        "author": "Custom",
        "desc": "恩山无线论坛(Right.com.cn)每日自动签到",
        "logo": "https://www.right.com.cn/favicon.ico"
    }

    # 配置定义
    def init_plugin(self, config: dict = None):
        self.cfg = config or {}
        self.cron = self.cfg.get("cron", "0 9 * * *")  # 默认每天上午9点
        self.cookie = self.cfg.get("cookie", "")
        
        # 注册定时任务
        self.register_scheduler(
            id="enshan_signin_job",
            func=self.sign_in,
            trigger=CronTrigger.from_crontab(self.cron)
        )

    def get_state(self):
        """
        获取插件运行状态
        """
        return True

    def stop_service(self):
        """
        停止插件服务（注销定时任务）
        """
        try:
            self.unregister_scheduler(id="enshan_signin_job")
        except Exception as e:
            logger.error(f"停止恩山签到服务失败: {e}")

    def sign_in(self):
        """
        执行签到逻辑
        """
        if not self.cookie:
            logger.error("【恩山签到】未配置Cookie，无法执行签到")
            return

        logger.info("【恩山签到】开始执行签到任务...")
        
        # 恩山通常需要模拟移动端或PC端的User-Agent
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": self.cookie,
            "Host": "www.right.com.cn",
            "Referer": "https://www.right.com.cn/forum/forum.php"
        }

        session = requests.Session()
        session.headers.update(headers)

        try:
            # 第一步：访问主页获取 formhash
            # Discuz 论坛的操作通常需要 formhash 来验证防止 CSRF
            index_url = "https://www.right.com.cn/forum/forum.php"
            resp = session.get(index_url, timeout=30)
            
            if "登录" in resp.text and "退出" not in resp.text:
                logger.error("【恩山签到】Cookie已失效，请重新获取")
                self.post_message(title="恩山签到失败", text="Cookie已失效，请重新配置。")
                return

            # 使用正则提取 formhash
            match = re.search(r'formhash=([a-zA-Z0-9]+)', resp.text)
            if not match:
                logger.error("【恩山签到】无法获取 formhash，可能是网站结构变更或被拦截")
                return
            
            formhash = match.group(1)
            logger.debug(f"【恩山签到】获取到 formhash: {formhash}")

            # 第二步：发送签到请求
            # 使用 DSU Paul Sign 插件接口
            sign_url = f"https://www.right.com.cn/forum/plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1&inajax=1"
            
            data = {
                "formhash": formhash,
                "qdxq": "kx",   # 心情：开心
                "qdmode": "1",  # 模式：打卡签到
                "todaysay": "Daily Checkin", # 每日一言
                "fastreply": "0"
            }

            sign_resp = session.post(sign_url, data=data, timeout=30)
            res_text = sign_resp.text

            # 第三步：解析结果
            if "恭喜你签到成功" in res_text or "已经签到" in res_text:
                logger.info("【恩山签到】签到成功")
                # 只有需要通知时才发送消息，避免打扰
                # self.post_message(title="恩山签到成功", text="今日签到任务已完成。") 
            elif "请稍后再试" in res_text:
                logger.warning("【恩山签到】操作频繁或需要验证码")
            else:
                logger.error(f"【恩山签到】未知响应: {res_text[:100]}")
                self.post_message(title="恩山签到异常", text=f"响应内容: {res_text[:100]}")

        except Exception as e:
            logger.error(f"【恩山签到】网络请求出错: {e}")
            self.post_message(title="恩山签到出错", text=str(e))

    def post_message(self, title, text):
        """
        发送通知消息
        """
        from app.utils.commons import SystemUtils
        # 使用 MoviePilot 的通知渠道
        SystemUtils.push_message(title=title, content=text)
