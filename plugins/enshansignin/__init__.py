import re
import requests
from typing import Any, List, Dict, Tuple
from apscheduler.triggers.cron import CronTrigger
from app.plugins import _PluginBase
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from app.log import logger

class EnshanSignin(_PluginBase):
    # 插件元数据
    plugin_name = "恩山论坛签到"
    plugin_desc = "恩山无线论坛(Right.com.cn)每日自动签到"
    plugin_icon = "https://raw.githubusercontent.com/kimjioo/MoviePilot-Plugins/main/icon/enshan.ico"
    plugin_version = "1.3"
    plugin_author = "kimjioo"
    author_url = "https://github.com/kimjioo"
    plugin_config_prefix = "enshansignin_"
    plugin_order = 10
    auth_level = 2

    # 私有属性
    _enabled = False
    _cookie = ""
    _cron = ""
    _notify = False

    def init_plugin(self, config: dict = None):
        """
        插件初始化
        """
        if config:
            self._enabled = config.get("enabled")
            self._cookie = config.get("cookie")
            self._cron = config.get("cron") or "0 9 * * *"
            self._notify = config.get("notify")

        # 停止现有任务
        self.stop_service()

        if self._enabled and self._cookie:
            try:
                self.register_scheduler(
                    id="enshan_signin_job",
                    func=self.sign_in,
                    trigger=CronTrigger.from_crontab(self._cron)
                )
                logger.info(f"【恩山签到】任务已加载，下次运行时间: {self._cron}")
            except Exception as e:
                logger.error(f"【恩山签到】定时任务注册失败: {e}")

    def get_state(self) -> bool:
        return self._enabled and bool(self._cookie)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '仅失败通知',
                                            'hint': '开启后，签到成功不发送通知'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cookie',
                                            'label': 'Cookie',
                                            'rows': 3,
                                            'placeholder': '复制请求头中的完整Cookie字符串',
                                            'required': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': 'Cron表达式',
                                            'placeholder': '0 9 * * *',
                                            'hint': '默认为每天 09:00'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "cookie": "",
            "cron": "0 9 * * *",
            "notify": False
        }

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        try:
            self.unregister_scheduler(id="enshan_signin_job")
        except Exception:
            pass

    def send_notification(self, title, text):
        """
        使用事件总线发送系统通知
        """
        try:
            eventmanager.send_event(
                EventType.NoticeMessage,
                {
                    "title": title,
                    "text": text
                }
            )
        except Exception as e:
            logger.error(f"【恩山签到】发送通知失败: {e}")

    def sign_in(self):
        """
        执行签到逻辑
        """
        if not self._cookie:
            return

        logger.info("【恩山签到】开始执行...")
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": self._cookie,
            "Host": "www.right.com.cn",
            "Referer": "https://www.right.com.cn/forum/forum.php"
        }

        session = requests.Session()
        session.headers.update(headers)

        try:
            # 1. 获取 formhash
            index_url = "https://www.right.com.cn/forum/forum.php"
            resp = session.get(index_url, timeout=30)
            
            if "登录" in resp.text and "退出" not in resp.text:
                logger.error("【恩山签到】Cookie已失效")
                self.send_notification("恩山签到失败", "Cookie已失效，请重新配置。")
                return

            match = re.search(r'formhash=([a-zA-Z0-9]+)', resp.text)
            if not match:
                logger.error("【恩山签到】无法获取 formhash")
                return
            
            formhash = match.group(1)

            # 2. 签到
            sign_url = f"https://www.right.com.cn/forum/plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1&inajax=1"
            data = {
                "formhash": formhash,
                "qdxq": "kx",
                "qdmode": "1",
                "todaysay": "Daily Checkin",
                "fastreply": "0"
            }

            sign_resp = session.post(sign_url, data=data, timeout=30)
            res_text = sign_resp.text

            # 3. 结果判断
            if "恭喜你签到成功" in res_text or "已经签到" in res_text:
                logger.info("【恩山签到】成功")
                if not self._notify:
                    self.send_notification("恩山签到成功", "今日签到任务已完成。")
            elif "请稍后再试" in res_text:
                logger.warning("【恩山签到】操作频繁")
            else:
                logger.error(f"【恩山签到】未知响应: {res_text[:50]}")
                self.send_notification("恩山签到异常", f"响应: {res_text[:50]}")

        except Exception as e:
            logger.error(f"【恩山签到】请求出错: {e}")
            self.send_notification("恩山签到出错", str(e))
