"""
DeepFlood论坛签到插件
版本: 1.1.1
作者: Madrays (Modified for DeepFlood)
功能:
- 自动完成DeepFlood论坛每日签到
- 支持选择随机奖励或固定奖励
- 自动失败重试机制
- 定时签到和历史记录
- 支持绕过CloudFlare防护
"""
import time
import random
import traceback
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
import requests
from urllib.parse import urlencode
import json

# cloudscraper 作为 Cloudflare 备用方案
try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except Exception:
    HAS_CLOUDSCRAPER = False

# 尝试导入curl_cffi库，用于绕过CloudFlare防护
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


class deepfloodsign(_PluginBase):
    # 插件名称
    plugin_name = "DeepFlood论坛签到"
    # 插件描述
    plugin_desc = "自动完成DeepFlood论坛每日签到，支持随机奖励和自动重试功能"
    # 插件图标
    plugin_icon = "https://www.deepflood.com/static/logo.png"
    # 插件版本
    plugin_version = "2.1.0"
    # 插件作者
    plugin_author = "madrays"
    # 作者主页
    author_url = "https://github.com/madrays"
    # 插件配置项ID前缀
    plugin_config_prefix = "deepfloodsign_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _cookie = None
    _notify = False
    _onlyonce = False
    _clear_history = False
    _cron = None
    _random_choice = True
    _history_days = 30
    _use_proxy = True
    _max_retries = 3
    _retry_count = 0
    _scheduled_retry = None
    _verify_ssl = False
    _min_delay = 5
    _max_delay = 12
    _member_id = ""
    _stats_days = 30

    _scraper = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _manual_trigger = False

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        logger.info("============= deepfloodsign 初始化 =============")
        try:
            if config:
                self._enabled = config.get("enabled")
                self._cookie = config.get("cookie")
                self._notify = config.get("notify")
                self._cron = config.get("cron")
                self._onlyonce = config.get("onlyonce")
                self._random_choice = config.get("random_choice")
                
                try:
                    self._history_days = int(config.get("history_days", 30))
                except (ValueError, TypeError):
                    self._history_days = 30
                
                self._use_proxy = config.get("use_proxy", True)
                
                try:
                    self._max_retries = int(config.get("max_retries", 3))
                except (ValueError, TypeError):
                    self._max_retries = 3
                
                self._verify_ssl = config.get("verify_ssl", False)
                
                try:
                    self._min_delay = int(config.get("min_delay", 5))
                except (ValueError, TypeError):
                    self._min_delay = 5
                
                try:
                    self._max_delay = int(config.get("max_delay", 12))
                except (ValueError, TypeError):
                    self._max_delay = 12
                self._member_id = (config.get("member_id") or "").strip()
                self._clear_history = config.get("clear_history", False)
                try:
                    self._stats_days = int(config.get("stats_days", 30))
                except (ValueError, TypeError):
                    self._stats_days = 30
                
                log_msg = (
                    f"配置: enabled={self._enabled}, notify={self._notify}, cron={self._cron}, "
                    f"random_choice={self._random_choice}, history_days={self._history_days}, "
                    f"use_proxy={self._use_proxy}, max_retries={self._max_retries}, "
                    f"verify_ssl={self._verify_ssl}, min_delay={self._min_delay}, "
                    f"max_delay={self._max_delay}, member_id={self._member_id or '未设置'}, "
                    f"clear_history={self._clear_history}"
                )
                logger.info(log_msg)

                # 初始化 cloudscraper
                if HAS_CLOUDSCRAPER:
                    try:
                        self._scraper = cloudscraper.create_scraper(browser="chrome")
                    except Exception:
                        try:
                            self._scraper = cloudscraper.create_scraper()
                        except Exception as e2:
                            logger.warning(f"cloudscraper 初始化失败: {str(e2)}")
                            self._scraper = None
                    if self._scraper:
                        proxies = self._get_proxies()
                        if proxies:
                            self._scraper.proxies = proxies
                            logger.info(f"cloudscraper 初始化代理: {self._scraper.proxies}")
                        logger.info("cloudscraper 初始化成功")
            
            if self._onlyonce:
                logger.info("执行一次性签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(func=self.sign, trigger='date',
                                   run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                   name="DeepFlood论坛签到")
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "enabled": self._enabled,
                    "cookie": self._cookie,
                    "notify": self._notify,
                    "cron": self._cron,
                    "random_choice": self._random_choice,
                    "history_days": self._history_days,
                    "use_proxy": self._use_proxy,
                    "max_retries": self._max_retries,
                    "verify_ssl": self._verify_ssl,
                    "min_delay": self._min_delay,
                    "max_delay": self._max_delay,
                    "member_id": self._member_id,
                    "clear_history": self._clear_history,
                    "stats_days": self._stats_days
                })

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

                if self._clear_history:
                    logger.info("检测到清除历史记录标志，开始清空数据...")
                    self.clear_sign_history()
                    logger.info("已清除签到历史记录")
                    self.update_config({
                        "onlyonce": False,
                        "enabled": self._enabled,
                        "cookie": self._cookie,
                        "notify": self._notify,
                        "cron": self._cron,
                        "random_choice": self._random_choice,
                        "history_days": self._history_days,
                        "use_proxy": self._use_proxy,
                        "max_retries": self._max_retries,
                        "verify_ssl": self._verify_ssl,
                        "min_delay": self._min_delay,
                        "max_delay": self._max_delay,
                        "member_id": self._member_id,
                        "clear_history": False,
                        "stats_days": self._stats_days
                    })
                    logger.info("已保存配置，clear_history 已重置为 False")

        except Exception as e:
            logger.error(f"deepfloodsign初始化错误: {str(e)}", exc_info=True)

    def sign(self):
        """
        执行DeepFlood签到
        """
        logger.info("============= 开始DeepFlood签到 =============")
        sign_dict = None
        
        try:
            if not self._cookie:
                logger.error("未配置Cookie")
                sign_dict = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败: 未配置Cookie",
                }
                self._save_sign_history(sign_dict)
                
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【DeepFlood论坛签到失败】",
                        text="未配置Cookie，请在设置中添加Cookie"
                    )
                return sign_dict
            
            self._wait_random_interval()
            
            result = self._run_api_sign()
            
            user_info = None
            try:
                if getattr(self, "_member_id", ""):
                    user_info = self._fetch_user_info(self._member_id)
            except Exception as e:
                logger.warning(f"获取用户信息失败: {str(e)}")
            
            attendance_record = None
            try:
                attendance_record = self._fetch_attendance_record()
            except Exception as e:
                logger.warning(f"获取签到记录失败: {str(e)}")
            
            if result["success"]:
                sign_dict = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到成功" if not result.get("already_signed") else "已签到",
                    "message": result.get("message", "")
                }
                
                if attendance_record and attendance_record.get("gain"):
                    sign_dict["gain"] = attendance_record.get("gain")
                    if attendance_record.get("rank"):
                        sign_dict["rank"] = attendance_record.get("rank")
                        sign_dict["total_signers"] = attendance_record.get("total_signers")
                elif result.get("gain"):
                    sign_dict["gain"] = result.get("gain")
                
                self._save_sign_history(sign_dict)
                self._save_last_sign_date()
                self._retry_count = 0

                if self._notify:
                    try:
                        self._send_sign_notification(sign_dict, result, user_info, attendance_record)
                        logger.info("签到成功通知发送成功")
                    except Exception as e:
                        logger.error(f"签到成功通知发送失败: {str(e)}")
                
                try:
                    stats = self._get_signin_stats(self._stats_days)
                    if stats:
                        self.save_data('last_signin_stats', stats)
                except Exception as e:
                    logger.warning(f"获取收益统计失败: {str(e)}")
            else:
                sign_dict = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败",
                    "message": result.get("message", "")
                }
                
                # 兜底逻辑
                try:
                    if attendance_record and attendance_record.get("created_at"):
                        record_date = datetime.fromisoformat(attendance_record["created_at"].replace('Z', '+00:00'))
                        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                        if record_date.date() == today.date():
                            logger.info(f"从签到记录确认今日已签到: {attendance_record}")
                            result["success"] = True
                            result["already_signed"] = True
                            result["message"] = "今日已签到（记录确认）"
                            sign_dict["status"] = "已签到（记录确认）"
                        else:
                            current_time = datetime.utcnow()
                            record_time = datetime.fromisoformat(attendance_record["created_at"].replace('Z', '+00:00')).replace(tzinfo=None)
                            time_diff = abs((current_time - record_time).total_seconds() / 3600)
                            if time_diff < 0.5:
                                logger.info("时间差 < 0.5h，作为最后兜底判定为成功")
                                result["success"] = True
                                result["signed"] = True
                                sign_dict["status"] = "签到成功（兜底时间验证）"
                                result["message"] = "签到成功（兜底时间验证）"
                except Exception as e:
                    logger.warning(f"兜底时间验证失败: {str(e)}")
                
                self._save_sign_history(sign_dict)
                try:
                    stats = self._get_signin_stats(self._stats_days)
                    if stats:
                        self.save_data('last_signin_stats', stats)
                except Exception as e:
                    logger.warning(f"获取收益统计失败: {str(e)}")
                
                max_retries = int(self._max_retries) if self._max_retries is not None else 0
                
                if max_retries and self._retry_count < max_retries:
                    self._retry_count += 1
                    retry_minutes = random.randint(5, 15)
                    retry_time = datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(minutes=retry_minutes)
                    
                    logger.info(f"签到失败，将在 {retry_minutes} 分钟后重试 (重试 {self._retry_count}/{max_retries})")
                    
                    if not self._scheduler:
                        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                        if not self._scheduler.running:
                            self._scheduler.start()
                    
                    if self._scheduled_retry:
                        try:
                            self._scheduler.remove_job(self._scheduled_retry)
                        except Exception as e:
                            logger.warning(f"移除旧任务时出错 (可忽略): {str(e)}")
                    
                    self._scheduled_retry = f"deepflood_retry_{int(time.time())}"
                    
                    # 修复处：将 name 参数的 f-string 拆分，避免 SyntaxError
                    job_name = f"DeepFlood论坛签到重试 {self._retry_count}/{max_retries}"
                    
                    self._scheduler.add_job(
                        func=self.sign,
                        trigger='date',
                        run_date=retry_time,
                        id=self._scheduled_retry,
                        name=job_name
                    )
                    
                    if self._notify:
                        msg_detail = result.get('message', '未知错误')
                        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        notify_text = f"签到失败: {msg_detail}\n将在 {retry_minutes} 分钟后进行第 {self._retry_count}/{max_retries} 次重试\n⏱️ {now_str}"
                        
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【DeepFlood论坛签到失败】",
                            text=notify_text
                        )
                else:
                    if max_retries == 0:
                        logger.info("未配置自动重试 (max_retries=0)，本次结束")
                    else:
                        logger.warning(f"已达到最大重试次数 ({max_retries})，今日不再重试")
                    
                    if self._notify:
                        retry_text = "未配置自动重试" if max_retries == 0 else f"已达到最大重试次数 ({max_retries})"
                        msg_detail = result.get('message', '未知错误')
                        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        notify_text = f"签到失败: {msg_detail}\n{retry_text}\n⏱️ {now_str}"
                        
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【DeepFlood论坛签到失败】",
                            text=notify_text
                        )
            
            return sign_dict
        
        except Exception as e:
            logger.error(f"DeepFlood签到过程中出错: {str(e)}", exc_info=True)
            
            sign_dict = {
                "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "status": f"签到出错: {str(e)}",
            }
            self._save_sign_history(sign_dict)
            
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【DeepFlood论坛签到出错】",
                    text=f"签到过程中出错: {str(e)}\n⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            
            return sign_dict
    
    def _run_api_sign(self):
        """
        使用API执行DeepFlood签到
        """
        try:
            result = {"success": False, "signed": False, "already_signed": False, "message": ""}
            headers = {
                'Accept': '*/*',
                'Accept-Encoding': 'gzip, deflate, br, zstd',
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Content-Length': '0',
                'Content-Type': 'application/json',
                'Origin': 'https://www.deepflood.com',
                'Referer': 'https://www.deepflood.com/board',
                'Sec-CH-UA': '"Chromium";v="136", "Not:A-Brand";v="24", "Google Chrome";v="136"',
                'Sec-CH-UA-Mobile': '?0',
                'Sec-CH-UA-Platform': '"Windows"',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
                'Cookie': self._cookie
            }
            random_param = "true" if self._random_choice else "false"
            url = f"https://www.deepflood.com/api/attendance?random={random_param}"
            proxies = self._get_proxies()
            response = self._smart_post(url=url, headers=headers, data=b'', proxies=proxies, timeout=30)
            try:
                data = response.json()
                msg = data.get('message', '')
                if data.get('success') is True:
                    result.update({"success": True, "signed": True, "message": msg})
                    gain = data.get('gain', 0)
                    current = data.get('current', 0)
                    if gain:
                        result.update({"gain": gain, "current": current})
                elif "积分" in msg or "奖励" in msg or "鸡腿" in msg:
                    result.update({"success": True, "signed": True, "message": msg})
                elif "已完成签到" in msg:
                    result.update({"success": True, "already_signed": True, "message": msg})
                elif msg == "USER NOT FOUND" or data.get('status') == 404:
                    result.update({"message": "Cookie已失效，请更新"})
                elif "签到" in msg and ("成功" in msg or "完成" in msg):
                    result.update({"success": True, "signed": True, "message": msg})
                else:
                    result.update({"message": msg or f"未知响应: {response.status_code}"})
            except Exception:
                text = response.text or ""
                try:
                    warm = self._scraper_warmup_and_attach_user_cookie()
                    if warm:
                        headers_retry = dict(headers)
                        headers_retry.pop('Cookie', None)
                        resp_retry = warm.post(url, headers=headers_retry, timeout=30)
                        ct_retry = resp_retry.headers.get('Content-Type', '')
                        if 'application/json' in (ct_retry or '').lower():
                            data = resp_retry.json()
                            msg = data.get('message', '')
                            if data.get('success') is True:
                                result.update({"success": True, "signed": True, "message": msg})
                                gain = data.get('gain', 0)
                                if gain:
                                    result.update({"gain": gain})
                                return result
                            elif "已完成签到" in msg:
                                result.update({"success": True, "already_signed": True, "message": msg})
                                return result
                except Exception:
                    pass
                if any(k in text for k in ["积分", "奖励", "签到成功", "签到完成", "success"]):
                    result.update({"success": True, "signed": True, "message": text[:80]})
                elif "已完成签到" in text:
                    result.update({"success": True, "already_signed": True, "message": text[:80]})
                elif "Cannot GET /api/attendance" in text:
                    result.update({"message": "服务端拒绝GET，需要POST；可能被WAF拦截"})
                elif any(k in text for k in ["登录", "注册", "你好啊，陌生人"]):
                    result.update({"message": "未登录或Cookie失效，返回登录页"})
                else:
                    result.update({"message": f"非JSON响应({response.status_code})"})
            return result
        except Exception as e:
            logger.error(f"API签到出错: {str(e)}", exc_info=True)
            return {"success": False, "message": f"API签到出错: {str(e)}"}

    def _scraper_warmup_and_attach_user_cookie(self):
        try:
            if not (HAS_CLOUDSCRAPER and self._scraper):
                return None
            proxies = self._get_proxies()
            if proxies:
                self._scraper.proxies = self._normalize_proxies(proxies) or {}
            self._scraper.get('https://www.deepflood.com/board', timeout=30)
            base = self._cookie or ''
            try:
                for part in base.split(';'):
                    kv = part.strip().split('=', 1)
                    if len(kv) == 2:
                        name, value = kv[0].strip(), kv[1].strip()
                        if name and value:
                            self._scraper.cookies.set(name, value, domain='www.deepflood.com')
            except Exception:
                pass
            return self._scraper
        except Exception:
            return None
    
    def _get_proxies(self):
        if not self._use_proxy:
            return None
        try:
            if hasattr(settings, 'PROXY') and settings.PROXY:
                norm = self._normalize_proxies(settings.PROXY)
                if norm:
                    return norm
            return None
        except Exception:
            return None

    def _normalize_proxies(self, proxies_input):
        try:
            if not proxies_input:
                return None
            if isinstance(proxies_input, str):
                return {"http": proxies_input, "https": proxies_input}
            if isinstance(proxies_input, dict):
                http_url = proxies_input.get("http") or proxies_input.get("HTTP") or proxies_input.get("https") or proxies_input.get("HTTPS")
                https_url = proxies_input.get("https") or proxies_input.get("HTTPS") or proxies_input.get("http") or proxies_input.get("HTTP")
                if not http_url and not https_url:
                    return None
                return {"http": http_url or https_url, "https": https_url or http_url}
        except Exception:
            pass
        return None

    def _wait_random_interval(self):
        try:
            min_delay = float(self._min_delay) if self._min_delay is not None else 5.0
            max_delay = float(self._max_delay) if self._max_delay is not None else 12.0
            if max_delay >= min_delay and min_delay > 0:
                delay = random.uniform(min_delay, max_delay)
                logger.info(f"请求前随机等待 {delay:.2f} 秒...")
                time.sleep(delay)
        except Exception:
            pass

    def _smart_post(self, url, headers=None, data=None, json=None, proxies=None, timeout=30):
        # 1) cloudscraper 优先
        if HAS_CLOUDSCRAPER and self._scraper:
            try:
                if proxies:
                    self._scraper.proxies = self._normalize_proxies(proxies) or {}
                resp = self._scraper.post(url, headers=headers, data=data, json=json, timeout=timeout) if not self._verify_ssl else self._scraper.post(url, headers=headers, data=data, json=json, timeout=timeout, verify=True)
                ct = resp.headers.get('Content-Type') or resp.headers.get('content-type') or ''
                if resp.status_code in (400, 403) or ('text/html' in ct.lower()):
                    pass
                else:
                    return resp
            except Exception:
                pass

        # 2) curl_cffi 次选
        if HAS_CURL_CFFI:
            try:
                session = curl_requests.Session(impersonate="chrome110")
                if proxies:
                    session.proxies = self._normalize_proxies(proxies) or {}
                resp = session.post(url, headers=headers, data=data, json=json, timeout=timeout) if not self._verify_ssl else session.post(url, headers=headers, data=data, json=json, timeout=timeout, verify=True)
                ct = resp.headers.get('Content-Type') or resp.headers.get('content-type') or ''
                if resp.status_code in (400, 403) or ('text/html' in ct.lower()):
                    if proxies:
                        try:
                            resp2 = session.post(url, headers=headers, data=data, json=json, timeout=timeout) if not self._verify_ssl else session.post(url, headers=headers, data=data, json=json, timeout=timeout, verify=True)
                            ct2 = resp2.headers.get('Content-Type') or resp2.headers.get('content-type') or ''
                            if resp2.status_code not in (400, 403) and ('text/html' not in ct2.lower()):
                                return resp2
                        except Exception:
                            pass
                else:
                    return resp
            except Exception:
                pass

        # 3) requests 兜底
        norm = self._normalize_proxies(proxies)
        resp = requests.post(url, headers=headers, data=data, json=json, proxies=norm, timeout=timeout) if not self._verify_ssl else requests.post(url, headers=headers, data=data, json=json, proxies=norm, timeout=timeout, verify=True)
        return resp

    def _smart_get(self, url, headers=None, proxies=None, timeout=30):
        if HAS_CLOUDSCRAPER and self._scraper:
            try:
                if proxies:
                    self._scraper.proxies = self._normalize_proxies(proxies) or {}
                resp = self._scraper.get(url, headers=headers, timeout=timeout) if not self._verify_ssl else self._scraper.get(url, headers=headers, timeout=timeout, verify=True)
                ct = resp.headers.get('Content-Type') or resp.headers.get('content-type') or ''
                if resp.status_code in (400, 403) or ('text/html' in ct.lower()):
                    pass
                else:
                    return resp
            except Exception:
                pass
        if HAS_CURL_CFFI:
            try:
                session = curl_requests.Session(impersonate="chrome110")
                if proxies:
                    session.proxies = self._normalize_proxies(proxies) or {}
                resp = session.get(url, headers=headers, timeout=timeout) if not self._verify_ssl else session.get(url, headers=headers, timeout=timeout, verify=True)
                ct = resp.headers.get('Content-Type') or resp.headers.get('content-type') or ''
                if resp.status_code in (400, 403) or ('text/html' in ct.lower()):
                    if proxies:
                        try:
                            resp2 = session.get(url, headers=headers, timeout=timeout) if not self._verify_ssl else session.get(url, headers=headers, timeout=timeout, verify=True)
                            ct2 = resp2.headers.get('Content-Type') or resp2.headers.get('content-type') or ''
                            if resp2.status_code not in (400, 403) and ('text/html' not in ct2.lower()):
                                return resp2
                        except Exception:
                            pass
                else:
                    return resp
            except Exception:
                pass
        
        norm = self._normalize_proxies(proxies)
        if self._verify_ssl:
            return requests.get(url, headers=headers, proxies=norm, timeout=timeout, verify=True)
        return requests.get(url, headers=headers, proxies=norm, timeout=timeout)

    def _fetch_user_info(self, member_id: str) -> dict:
        if not member_id:
            return {}
        url = f"https://www.deepflood.com/api/account/getInfo/{member_id}?readme=1"
        headers = {
            "Accept": "*/*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        }
        proxies = self._get_proxies()
        resp = self._smart_get(url=url, headers=headers, proxies=proxies, timeout=30)
        try:
            data = resp.json()
            detail = data.get("detail") or {}
            if detail:
                self.save_data('last_user_info', detail)
            return detail
        except Exception:
            return {}

    def _fetch_attendance_record(self) -> dict:
        try:
            url = "https://www.deepflood.com/api/attendance/board?page=1"
            headers = {
                "Accept": "*/*",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                "Cookie": self._cookie
            }
            proxies = self._get_proxies()
            resp = self._smart_get(url=url, headers=headers, proxies=proxies, timeout=30)
            
            content_encoding = resp.headers.get('content-encoding', '').lower()
            if content_encoding == 'br':
                try:
                    import brotli
                    decompressed_content = brotli.decompress(resp.content)
                    response_text = decompressed_content.decode('utf-8')
                except ImportError:
                    response_text = resp.text
                except Exception:
                    response_text = resp.text
            else:
                response_text = resp.text
            
            data = None
            try:
                data = resp.json()
            except Exception:
                try:
                    data = json.loads(response_text or "")
                except Exception:
                    cached = self.get_data('last_attendance_record') or {}
                    return cached or {}
            record = data.get("record", {})
            if record:
                if "order" in data:
                    record['rank'] = data.get("order")
                    record['total_signers'] = data.get("total")
                self.save_data('last_attendance_record', record)
            return record
        except Exception:
            return {}

    def _save_sign_history(self, sign_data):
        try:
            history = self.get_data('sign_history') or []
            if "date" not in sign_data:
                sign_data["date"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                
            history.append(sign_data)
            
            try:
                retention_days = int(self._history_days) if self._history_days is not None else 30
            except (ValueError, TypeError):
                retention_days = 30
            
            now = datetime.now()
            valid_history = []
            
            for i, record in enumerate(history):
                try:
                    record_date = datetime.strptime(record["date"], '%Y-%m-%d %H:%M:%S')
                    days_diff = (now - record_date).days
                    if days_diff < retention_days:
                        valid_history.append(record)
                except (ValueError, KeyError):
                    record["date"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    valid_history.append(record)
            
            self.save_data(key="sign_history", value=valid_history)
        except Exception as e:
            logger.error(f"保存签到历史记录失败: {str(e)}")

    def clear_sign_history(self):
        try:
            self.save_data(key="sign_history", value=[])
            self.save_data(key="last_sign_date", value="")
            self.save_data(key="last_user_info", value="")
            self.save_data(key="last_attendance_record", value="")
        except Exception:
            pass

    def _send_sign_notification(self, sign_dict, result, user_info: dict = None, attendance_record: dict = None):
        if not self._notify:
            return
            
        status = sign_dict.get("status", "未知")
        sign_time = sign_dict.get("date", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        
        if "签到成功" in status:
            title = "【✅ DeepFlood论坛签到成功】"
            gain_info = ""
            rank_info = ""
            try:
                if result.get("gain"):
                    gain_info = f"🎁 获得: {result.get('gain')}个积分"
                elif attendance_record and attendance_record.get("gain"):
                    gain_info = f"🎁 今日获得: {attendance_record.get('gain')}个积分"
                
                if attendance_record:
                    if attendance_record.get("rank"):
                        rank_info = f"🏆 排名: 第{attendance_record.get('rank')}名"
                        if attendance_record.get("total_signers"):
                            rank_info += f" (共{attendance_record.get('total_signers')}人)"
                    elif attendance_record.get("total_signers"):
                        rank_info = f"📊 今日共{attendance_record.get('total_signers')}人签到"
                
                if rank_info:
                    gain_info = f"{gain_info}\n{rank_info}\n"
                else:
                    gain_info = f"{gain_info}\n"
            except Exception:
                gain_info = ""
            
            user_info_text = ""
            if user_info:
                member_name = user_info.get('member_name', '未知')
                rank = user_info.get('rank', '未知')
                coin = user_info.get('coin', '未知')
                user_info_text = f"👤 用户：{member_name}  等级：{rank}  积分：{coin}\n"
            
            text_parts = [
                f"📢 执行结果",
                f"━━━━━━━━━━",
                f"🕐 时间：{sign_time}",
                f"✨ 状态：{status}",
                user_info_text.rstrip('\n') if user_info_text else "",
                gain_info.rstrip('\n') if gain_info else "",
                f"━━━━━━━━━━"
            ]
            text = "\n".join([part for part in text_parts if part])
            
        elif "已签到" in status:
            title = "【ℹ️ DeepFlood论坛今日已签到】"
            gain_info = ""
            rank_info = ""
            try:
                today_gain = None
                if attendance_record and attendance_record.get("gain"):
                    today_gain = attendance_record.get('gain')
                elif result and result.get("gain"):
                    today_gain = result.get("gain")
                
                if today_gain is not None:
                    gain_info = f"🎁 今日获得: {today_gain}个积分"
                
                if attendance_record.get("rank"):
                    rank_info = f"🏆 排名: 第{attendance_record.get('rank')}名"
                    if attendance_record.get("total_signers"):
                        rank_info += f" (共{attendance_record.get('total_signers')}人)"
                elif attendance_record.get("total_signers"):
                    rank_info = f"📊 今日共{attendance_record.get('total_signers')}人签到"
                
                if rank_info:
                    gain_info = f"{gain_info}\n{rank_info}\n"
                else:
                    gain_info = f"{gain_info}\n"
            except Exception:
                gain_info = ""
            
            user_info_text = ""
            if user_info:
                member_name = user_info.get('member_name', '未知')
                rank = user_info.get('rank', '未知')
                coin = user_info.get('coin', '未知')
                user_info_text = f"👤 用户：{member_name}  等级：{rank}  积分：{coin}\n"
            
            text_parts = [
                f"📢 执行结果",
                f"━━━━━━━━━━",
                f"🕐 时间：{sign_time}",
                f"✨ 状态：{status}",
                user_info_text.rstrip('\n') if user_info_text else "",
                gain_info.rstrip('\n') if gain_info else "",
                f"ℹ️ 说明：今日已完成签到，显示当前状态和奖励信息",
                f"━━━━━━━━━━"
            ]
            text = "\n".join([part for part in text_parts if part])
            
        else:
            title = "【❌ DeepFlood论坛签到失败】"
            record_info = ""
            try:
                if attendance_record and attendance_record.get("created_at"):
                    record_date = datetime.fromisoformat(attendance_record["created_at"].replace('Z', '+00:00'))
                    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    if record_date.date() == today.date():
                        record_info = f"📊 签到记录: 今日已获得{attendance_record.get('gain', 0)}个积分"
                        if attendance_record.get("rank"):
                            record_info += f"，排名第{attendance_record.get('rank')}名"
                        record_info += "\n"
            except Exception:
                pass
            
            text_parts = [
                f"📢 执行结果",
                f"━━━━━━━━━━",
                f"🕐 时间：{sign_time}",
                f"❌ 状态：{status}",
                record_info.rstrip('\n') if record_info else "",
                f"━━━━━━━━━━",
                f"💡 可能的解决方法",
                f"• 检查Cookie是否过期",
                f"• 确认站点是否可访问",
                f"━━━━━━━━━━"
            ]
            text = "\n".join([part for part in text_parts if part])
            
        try:
            self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)
        except Exception as e:
            logger.error(f"通知发送失败: {str(e)}")
    
    def _save_last_sign_date(self):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.save_data('last_sign_date', now)
        
    def _is_already_signed_today(self):
        today = datetime.now().strftime('%Y-%m-%d')
        history = self.get_data('sign_history') or []
        today_records = [
            record for record in history 
            if record.get("date", "").startswith(today) 
            and record.get("status") in ["签到成功", "已签到"]
        ]
        if today_records:
            return True
        last_sign_date = self.get_data('last_sign_date')
        if last_sign_date:
            try:
                last_sign_datetime = datetime.strptime(last_sign_date, '%Y-%m-%d %H:%M:%S')
                if last_sign_datetime.strftime('%Y-%m-%d') == today:
                    return True
            except Exception:
                pass
        return False

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "deepfloodsign",
                "name": "DeepFlood论坛签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sign,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        curl_cffi_status = "✅ 已安装" if HAS_CURL_CFFI else "❌ 未安装"
        cloudscraper_status = "✅ 已启用" if HAS_CLOUDSCRAPER else "❌ 未启用"
        
        help_text = (
            f'【使用教程】\n'
            f'1. 登录DeepFlood论坛网站，按F12打开开发者工具\n'
            f'2. 在"网络"或"应用"选项卡中复制Cookie\n'
            f'3. 粘贴Cookie到上方输入框\n'
            f'4. 设置签到时间，建议早上8点(0 8 * * *)\n'
            f'5. 启用插件并保存\n\n'
            f'【功能说明】\n'
            f'• 随机奖励：开启则使用随机奖励，关闭则使用固定奖励\n'
            f'• 使用代理：开启则使用系统配置的代理服务器访问DeepFlood\n'
            f'• 验证SSL证书：关闭可能解决SSL连接问题，但会降低安全性\n'
            f'• 失败重试：设置签到失败后的最大重试次数，将在5-15分钟后随机重试\n'
            f'• 随机延迟：请求前随机等待，降低被风控概率\n'
            f'• 用户信息：配置成员ID后，通知中展示用户名/等级/积分\n'
            f'• 立即运行一次：手动触发一次签到\n'
            f'• 清除历史记录：勾选后保存配置，插件将清空所有数据\n\n'
            f'【环境状态】\n'
            f'• curl_cffi: {curl_cffi_status}；cloudscraper: {cloudscraper_status}'
        )
        
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '开启通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'random_choice', 'label': '随机奖励'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'use_proxy', 'label': '使用代理'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'verify_ssl', 'label': '验证SSL证书'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'clear_history', 'label': '清除历史记录'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VTextField', 'props': {'model': 'member_id', 'label': '成员ID（可选）', 'placeholder': '用于获取用户信息'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'min_delay', 'label': '最小随机延迟(秒)', 'type': 'number', 'placeholder': '5'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'max_delay', 'label': '最大随机延迟(秒)', 'type': 'number', 'placeholder': '12'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'cookie', 'label': '站点Cookie', 'placeholder': '请输入站点Cookie值'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '签到周期'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'history_days', 'label': '历史保留天数', 'type': 'number', 'placeholder': '30'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'max_retries', 'label': '失败重试次数', 'type': 'number', 'placeholder': '3'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VTextField', 'props': {'model': 'stats_days', 'label': '收益统计天数', 'type': 'number', 'placeholder': '30'}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': help_text}}]}
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cookie": "",
            "cron": "0 8 * * *",
            "random_choice": True,
            "history_days": 30,
            "use_proxy": True,
            "max_retries": 3,
            "verify_ssl": False,
            "min_delay": 5,
            "max_delay": 12,
            "member_id": "",
            "clear_history": False,
            "stats_days": 30
        }

    def get_page(self) -> List[dict]:
        user_info = self.get_data('last_user_info') or {}
        historys = self.get_data('sign_history') or []
        
        if not historys:
            return [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '暂无签到记录，请先配置Cookie并启用插件', 'class': 'mb-2'}}]
        
        historys = sorted(historys, key=lambda x: x.get("date", ""), reverse=True)
        history_rows = []
        for history in historys:
            status_text = history.get("status", "未知")
            success_statuses = ["签到成功", "已签到", "签到成功（时间验证）", "已签到（从记录确认）"]
            status_color = "success" if any(s in status_text for s in success_statuses) else "error"
            
            reward_info = "-"
            try:
                if any(success_status in status_text for success_status in success_statuses):
                    if "gain" in history:
                        reward_info = f"{history.get('gain', 0)}个积分"
                        if "rank" in history and "total_signers" in history:
                            reward_info += f" (第{history.get('rank')}名，共{history.get('total_signers')}人)"
                    else:
                        attendance_record = self.get_data('last_attendance_record') or {}
                        if attendance_record and attendance_record.get('gain'):
                            reward_info = f"{attendance_record.get('gain')}个积分"
                            if attendance_record.get('rank') and attendance_record.get('total_signers'):
                                reward_info += f" (第{attendance_record.get('rank')}名，共{attendance_record.get('total_signers')}人)"
            except Exception:
                reward_info = "-"
            
            history_rows.append({
                'component': 'tr',
                'content': [
                    {'component': 'td', 'props': {'class': 'text-caption'}, 'text': history.get("date", "")},
                    {'component': 'td', 'content': [{'component': 'VChip', 'props': {'color': status_color, 'size': 'small', 'variant': 'outlined'}, 'text': status_text}]},
                    {'component': 'td', 'content': [{'component': 'VChip', 'props': {'color': 'amber-darken-2' if reward_info != "-" else 'grey', 'size': 'small', 'variant': 'outlined'}, 'text': reward_info}]},
                    {'component': 'td', 'text': history.get('message', '-')}
                ]
            })
        
        user_info_card = []
        member_id = ""
        avatar_url = None
        user_name = "-"
        rank = "-"
        coin = "-"
        npost = "-"
        ncomment = "-"
        sign_rank = None
        total_signers = None
        
        if user_info:
            member_id = str(user_info.get('member_id') or getattr(self, '_member_id', '') or '').strip()
            avatar_url = f"https://www.deepflood.com/avatar/{member_id}.png" if member_id else None
            user_name = user_info.get('member_name', '-')
            rank = str(user_info.get('rank', '-'))
            coin = str(user_info.get('coin', '-'))
            npost = str(user_info.get('nPost', '-'))
            ncomment = str(user_info.get('nComment', '-'))
            
            attendance_record = self.get_data('last_attendance_record') or {}
            sign_rank = attendance_record.get('rank')
            total_signers = attendance_record.get('total_signers')
            
            user_info_card = [
                {
                    'component': 'VCard',
                    'props': {'variant': 'outlined', 'class': 'mb-4'},
                    'content': [
                        {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '👤 DeepFlood 用户信息'},
                        {
                            'component': 'VCardText',
                            'content': [
                                {
                                    'component': 'VRow',
                                    'props': {'align': 'center'},
                                    'content': [
                                        {
                                            'component': 'VCol',
                                            'props': {'cols': 12, 'md': 2},
                                            'content': [
                                                (
                                                    {'component': 'VAvatar', 'props': {'size': 72, 'class': 'mx-auto'}, 'content': [{'component': 'VImg', 'props': {'src': avatar_url}}]} if avatar_url else {'component': 'VAvatar', 'props': {'size': 72, 'color': 'grey-lighten-2', 'class': 'mx-auto'}, 'text': user_name[:1]}
                                                )
                                            ]
                                        },
                                        {
                                            'component': 'VCol',
                                            'props': {'cols': 12, 'md': 10},
                                            'content': [
                                                {
                                                    'component': 'VRow',
                                                    'props': {'class': 'mb-2'},
                                                    'content': [
                                                        {'component': 'span', 'props': {'class': 'text-subtitle-1 mr-4'}, 'text': user_name},
                                                        {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': 'primary', 'class': 'mr-2'}, 'text': f'等级 {rank}'},
                                                        {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': 'amber-darken-2', 'class': 'mr-2'}, 'text': f'积分 {coin}'},
                                                        {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'class': 'mr-2'}, 'text': f'主题 {npost}'},
                                                        {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined'}, 'text': f'评论 {ncomment}'}
                                                    ] + ([
                                                        {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': 'success', 'class': 'mr-2'}, 'text': f'签到排名 {sign_rank}'},
                                                        {'component': 'VChip', 'props': {'size': 'small', 'variant': 'outlined', 'color': 'info', 'class': 'mr-2'}, 'text': f'总人数 {total_signers}'}
                                                    ] if sign_rank and total_signers else [])
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]

        stats = self.get_data('last_signin_stats') or {}
        stats_card = []
        if stats:
            period = stats.get('period') or f"近{self._stats_days}天"
            days_count = stats.get('days_count', 0)
            total_amount = stats.get('total_amount', 0)
            average = stats.get('average', 0)
            stats_card = [
                {
                    'component': 'VCard',
                    'props': {'variant': 'outlined', 'class': 'mb-4'},
                    'content': [
                        {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📈 DeepFlood收益统计'},
                        {
                            'component': 'VCardText',
                            'content': [
                                {'component': 'div', 'props': {'class': 'mb-2'}, 'text': f'{period} 已签到 {days_count} 天'},
                                {
                                    'component': 'VRow',
                                    'content': [
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined', 'color': 'amber-darken-2'}, 'text': f'总积分 {total_amount}'}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined', 'color': 'primary'}, 'text': f'平均/日 {average}'}]},
                                        {'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [{'component': 'VChip', 'props': {'variant': 'outlined'}, 'text': f'统计天数 {days_count}'}]},
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]

        return user_info_card + stats_card + [
            {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'mb-4'},
                'content': [
                    {'component': 'VCardTitle', 'props': {'class': 'text-h6'}, 'text': '📊 DeepFlood论坛签到历史'},
                    {'component': 'VCardText', 'content': [{'component': 'VTable', 'props': {'hover': True, 'density': 'compact'}, 'content': [{'component': 'thead', 'content': [{'component': 'tr', 'content': [{'component': 'th', 'text': '时间'}, {'component': 'th', 'text': '状态'}, {'component': 'th', 'text': '奖励'}, {'component': 'th', 'text': '消息'}]}]}, {'component': 'tbody', 'content': history_rows}]}]}
                ]
            }
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception:
            pass

    def get_command(self) -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [] 

    def _get_signin_stats(self, days: int = 30) -> dict:
        if not self._cookie:
            return {}
        if days <= 0:
            days = 1
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
            'origin': 'https://www.deepflood.com',
            'referer': 'https://www.deepflood.com/board',
            'Cookie': self._cookie
        }
        tz = pytz.timezone('Asia/Shanghai')
        now_shanghai = datetime.now(tz)
        query_start_time = now_shanghai - timedelta(days=days)
        all_records = []
        page = 1
        proxies = self._get_proxies()
        try:
            while page <= 20:
                url = f'https://www.deepflood.com/api/account/credit/page-{page}'
                resp = self._smart_get(url=url, headers=headers, proxies=proxies, timeout=30)
                data = {}
                try:
                    data = resp.json()
                except Exception:
                    break
                if not data.get('success') or not data.get('data'):
                    break
                records = data.get('data', [])
                if not records:
                    break
                try:
                    last_record_time = datetime.fromisoformat(records[-1][3].replace('Z', '+00:00')).astimezone(tz)
                except Exception:
                    break
                if last_record_time < query_start_time:
                    for record in records:
                        try:
                            record_time = datetime.fromisoformat(record[3].replace('Z', '+00:00')).astimezone(tz)
                        except Exception:
                            continue
                        if record_time >= query_start_time:
                            all_records.append(record)
                    break
                else:
                    all_records.extend(records)
                page += 1
        except Exception:
            pass
        signin_records = []
        for record in all_records:
            try:
                amount, balance, description, timestamp = record
                record_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).astimezone(tz)
            except Exception:
                continue
            if record_time >= query_start_time and ('签到收益' in description and ('积分' in description or '奖励' in description or '鸡腿' in description)):
                signin_records.append({'amount': amount, 'date': record_time.strftime('%Y-%m-%d'), 'description': description})
        period_desc = f'近{days}天' if days != 1 else '今天'
        if not signin_records:
            try:
                history = self.get_data('sign_history') or []
                success_statuses = ["签到成功", "已签到", "签到成功（时间验证）", "已签到（从记录确认）"]
                fallback_records = []
                for rec in history:
                    try:
                        rec_dt = datetime.strptime(rec.get('date', ''), '%Y-%m-%d %H:%M:%S').astimezone(tz)
                    except Exception:
                        continue
                    if rec_dt >= query_start_time and rec.get('status') in success_statuses and rec.get('gain'):
                        fallback_records.append({'amount': rec.get('gain', 0), 'date': rec_dt.strftime('%Y-%m-%d'), 'description': '本地历史-签到收益'})
                if not fallback_records:
                    return {'total_amount': 0, 'average': 0, 'days_count': 0, 'records': [], 'period': period_desc}
                total_amount = sum(r['amount'] for r in fallback_records)
                days_count = len(fallback_records)
                average = round(total_amount / days_count, 2) if days_count > 0 else 0
                return {'total_amount': total_amount, 'average': average, 'days_count': days_count, 'records': fallback_records, 'period': period_desc}
            except Exception:
                return {'total_amount': 0, 'average': 0, 'days_count': 0, 'records': [], 'period': period_desc}
        total_amount = sum(r['amount'] for r in signin_records)
        days_count = len(signin_records)
        average = round(total_amount / days_count, 2) if days_count > 0 else 0
        return {'total_amount': total_amount, 'average': average, 'days_count': days_count, 'records': signin_records, 'period': period_desc}
