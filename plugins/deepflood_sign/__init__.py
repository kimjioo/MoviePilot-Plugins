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
                    cached = self.get_data
