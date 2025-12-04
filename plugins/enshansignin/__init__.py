import re
import requests
from typing import Any, List, Dict, Tuple
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.core.event import eventmanager, Event
from app.log import logger
from app.utils.commons import SystemUtils

class EnshanSignin(_PluginBase):
    # 插件名称
    plugin_name = "恩山论坛签到"
    # 插件描述
    plugin_desc = "恩山无线论坛(Right.com.cn)每日自动签到，支持心情打卡。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/kimjioo/MoviePilot-Plugins/main/icon/enshan.ico"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "kimjioo"
    # 作者主页
    author_url = "https://github.com/kimjioo"
    # 插件配置项ID前缀
    plugin_config_prefix = "enshansignin_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _cookie = ""
    _cron = ""
    _notify = False

    def init_plugin(self, config: dict = None):
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
                                            'hint': '开启后，签到成功不发送通知，仅在失败或Cookie失效时通知'
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
        """
        停止插件服务
        """
        try:
            self.unregister_scheduler(id="enshan_signin_job")
        except Exception:
            pass

    def sign_in(self):
        """
        执行签到逻辑
        """
        if not self._cookie:
            return

        logger.info("【恩山签到】开始执行签到任务...")
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Cookie": self._cookie,
            "Host": "www.right.com.cn",
            "Referer": "https://www.right.com.cn/forum/forum.php"
        }

        session = requests.Session()
        session.headers.update(headers)

        try:
            # 第一步：访问主页获取 formhash
            index_url = "https://www.right.com.cn/forum/forum.php"
            resp = session.get(index_url, timeout=30)
            
            if "登录" in resp.text and "退出" not in resp.text:
                logger.error("【恩山签到】Cookie已失效")
                SystemUtils.push_message(title="恩山签到失败", content="Cookie已失效，请重新配置。")
                return

            match = re.search(r'formhash=([a-zA-Z0-9]+)', resp.text)
            if not match:
                logger.error("【恩山签到】无法获取 formhash")
                return
            
            formhash = match.group(1)

            # 第二步：发送签到请求
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

            # 第三步：解析结果
            if "恭喜你签到成功" in res_text or "已经签到" in res_text:
                logger.info("【恩山签到】签到成功")
                if not self._notify:
                    SystemUtils.push_message(title="恩山签到成功", content="今日签到任务已完成。")
            elif "请稍后再试" in res_text:
                logger.warning("【恩山签到】操作频繁或需要验证码")
            else:
                logger.error(f"【恩山签到】未知响应: {res_text[:100]}")
                SystemUtils.push_message(title="恩山签到异常", content=f"响应内容: {res_text[:100]}")

        except Exception as e:
            logger.error(f"【恩山签到】网络请求出错: {e}")
            SystemUtils.push_message(title="恩山签到出错", content=str(e))
