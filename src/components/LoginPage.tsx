import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { 
  Smartphone, 
  QrCode, 
  ShieldCheck, 
  MessageCircle, 
  ArrowRight, 
  Loader2, 
  CheckCircle2, 
  Sparkles, 
  Lock,
  User,
  AlertCircle
} from 'lucide-react';

interface LoginPageProps {
  onLoginSuccess: (userInfo: { phone?: string; loginMethod: 'sms' | 'wechat'; nickname: string }) => void;
}

export default function LoginPage({ onLoginSuccess }: LoginPageProps) {
  const [loginMethod, setLoginMethod] = useState<'sms' | 'wechat'>('sms');
  
  // SMS States
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [countdown, setCountdown] = useState(0);
  const [smsError, setSmsError] = useState('');
  const [smsSentNotice, setSmsSentNotice] = useState<string | null>(null);
  const [isSmsSubmitting, setIsSmsSubmitting] = useState(false);

  // WeChat States
  const [wechatStatus, setWechatStatus] = useState<'idle' | 'scanning' | 'scanned' | 'success'>('idle');
  const [wechatProgress, setWechatProgress] = useState(100); // QR code validity count down (seconds)
  const [wechatNotice, setWechatNotice] = useState('请使用微信扫一扫登录');

  // Triggered when countdown active
  useEffect(() => {
    if (countdown > 0) {
      const timer = setTimeout(() => setCountdown(countdown - 1), 1000);
      return () => clearTimeout(timer);
    }
  }, [countdown]);

  // QR Code expiration countdown and simulated auto-scanning
  useEffect(() => {
    let progressTimer: NodeJS.Timeout;
    if (loginMethod === 'wechat') {
      // Countdown for QR code expiration
      progressTimer = setInterval(() => {
        setWechatProgress(prev => {
          if (prev <= 1) {
            setWechatNotice('二维码已过期，请刷新');
            return 0;
          }
          return prev - 1;
        });
      }, 1000);

      // Simulate step-by-step WeChat scan states for extreme premium feel!
      setWechatStatus('idle');
      setWechatNotice('请使用微信「扫一扫」安全登录');
      
      const scanTimeout = setTimeout(() => {
        setWechatStatus('scanning');
        setWechatNotice('已检测到二维码被扫描，正在等待确认...');
        
        const confirmTimeout = setTimeout(() => {
          setWechatStatus('scanned');
          setWechatNotice('微信端授权成功，正在安全登录中...');
          
          const successTimeout = setTimeout(() => {
            setWechatStatus('success');
            onLoginSuccess({
              loginMethod: 'wechat',
              nickname: '微信用户_Anker'
            });
          }, 1500);

          return () => clearTimeout(successTimeout);
        }, 3000);

        return () => clearTimeout(confirmTimeout);
      }, 4000);

      return () => {
        clearInterval(progressTimer);
        clearTimeout(scanTimeout);
      };
    } else {
      setWechatProgress(100);
    }
  }, [loginMethod]);

  // Handle Send Verification Code
  const handleSendCode = () => {
    setSmsError('');
    // China mobile phone verification
    const phoneRegex = /^1[3-9]\d{9}$/;
    if (!phoneRegex.test(phone)) {
      setSmsError('请输入正确的11位中国手机号码');
      return;
    }

    // Generate random 6-digit verification code
    const generatedCode = Math.floor(100000 + Math.random() * 900000).toString();
    setCountdown(60);
    
    // Simulate real SMS gateway receipt by placing a tactile toast notice!
    setSmsSentNotice(generatedCode);
    
    // Auto fill for convenience or let the user enter it
    setTimeout(() => {
      setCode(generatedCode);
    }, 1500);
  };

  // Handle SMS Login Submit
  const handleSmsLogin = (e: React.FormEvent) => {
    e.preventDefault();
    setSmsError('');

    if (!phone) {
      setSmsError('手机号不能为空');
      return;
    }
    const phoneRegex = /^1[3-9]\d{9}$/;
    if (!phoneRegex.test(phone)) {
      setSmsError('请输入有效的11位中国手机号码');
      return;
    }
    if (!code || code.length < 4) {
      setSmsError('请输入正确的短信验证码');
      return;
    }

    setIsSmsSubmitting(true);
    
    // Simulate API delay
    setTimeout(() => {
      setIsSmsSubmitting(false);
      onLoginSuccess({
        phone: phone,
        loginMethod: 'sms',
        nickname: `手机用户_${phone.slice(-4)}`
      });
    }, 1200);
  };

  // Instant simulation login helper
  const handleInstantWechatLogin = () => {
    setWechatStatus('success');
    onLoginSuccess({
      loginMethod: 'wechat',
      nickname: '微信快捷登录用户'
    });
  };

  return (
    <div className="min-h-screen w-full flex bg-slate-50 relative overflow-hidden font-sans selection:bg-indigo-100 selection:text-indigo-900">
      
      {/* Decorative ambient blobs */}
      <div className="absolute top-[-20%] left-[-10%] w-[50%] h-[50%] bg-indigo-200/30 rounded-full blur-[120px] pointer-events-none" />
      <div className="absolute bottom-[-10%] right-[-10%] w-[45%] h-[45%] bg-pink-100/30 rounded-full blur-[100px] pointer-events-none" />

      {/* Grid Layout Container */}
      <div className="grid grid-cols-1 lg:grid-cols-12 w-full max-w-7xl mx-auto px-4 py-8 lg:py-16 gap-8 items-center justify-center relative z-10">
        
        {/* Left Side: Campaign Brand Visual Board */}
        <div className="lg:col-span-5 space-y-6 text-slate-800 pr-0 lg:pr-8 hidden lg:block">
          <div className="flex items-center gap-2">
            <div className="w-10 h-10 bg-indigo-600 rounded-xl flex items-center justify-center text-white font-extrabold text-xl shadow-lg shadow-indigo-600/20">
              A
            </div>
            <div>
              <h1 className="font-extrabold text-slate-900 text-lg tracking-tight font-display">KOL Insight AI</h1>
              <p className="text-[10px] text-slate-400 font-semibold tracking-wider uppercase">品牌声量与红人决策中心</p>
            </div>
          </div>

          <div className="space-y-4">
            <h2 className="text-3xl font-extrabold text-slate-900 leading-tight font-display tracking-tight">
              数据驱动的 <br/>
              <span className="text-transparent bg-clip-text bg-gradient-to-r from-indigo-600 to-pink-500">红人与社交传播</span> <br/>
              分析大盘
            </h2>
            <p className="text-xs text-slate-500 leading-relaxed max-w-sm">
              专为品牌主量身定制，提供舆情正向率、各平台达人传播周期、日度曝光波峰及多维受众统计画像分析，辅助精准投放决策。
            </p>
          </div>

          {/* Interactive Bullet points */}
          <div className="space-y-3 pt-2">
            <div className="flex items-start gap-2.5">
              <div className="h-5 w-5 rounded-full bg-indigo-50 flex items-center justify-center text-indigo-600 shrink-0 mt-0.5">
                <CheckCircle2 className="h-3.5 w-3.5" />
              </div>
              <div>
                <p className="text-xs font-semibold text-slate-800">全网声量监控与情感趋势</p>
                <p className="text-[10px] text-slate-400">一键洞悉全渠道正向/负面舆论分布特征。</p>
              </div>
            </div>

            <div className="flex items-start gap-2.5">
              <div className="h-5 w-5 rounded-full bg-pink-50 flex items-center justify-center text-pink-500 shrink-0 mt-0.5">
                <CheckCircle2 className="h-3.5 w-3.5" />
              </div>
              <div>
                <p className="text-xs font-semibold text-slate-800">去重受众统计画像</p>
                <p className="text-[10px] text-slate-400">聚合多平台红人粉丝客群，展示年龄与地域占比。</p>
              </div>
            </div>

            <div className="flex items-start gap-2.5">
              <div className="h-5 w-5 rounded-full bg-emerald-50 flex items-center justify-center text-emerald-500 shrink-0 mt-0.5">
                <CheckCircle2 className="h-3.5 w-3.5" />
              </div>
              <div>
                <p className="text-xs font-semibold text-slate-800">KOL 精细化贡献模型</p>
                <p className="text-[10px] text-slate-400">摆脱低效的ROI或硬性转化评定，精准提炼声量贡献。</p>
              </div>
            </div>
          </div>

          <div className="pt-6 border-t border-slate-200/60 flex items-center gap-2">
            <span className="inline-block h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wide">
              系统当前状态: 安全连接部署已就绪
            </span>
          </div>
        </div>

        {/* Right Side: Login Selector Card */}
        <div className="lg:col-span-7 flex justify-center">
          <motion.div 
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, ease: 'easeOut' }}
            className="w-full max-w-md bg-white rounded-2xl border border-slate-200/80 shadow-xl shadow-slate-100 p-6 md:p-8 space-y-6 relative overflow-hidden"
          >
            {/* Header mobile title */}
            <div className="lg:hidden flex items-center gap-2 mb-4 justify-center">
              <div className="w-8 h-8 bg-indigo-600 rounded-lg flex items-center justify-center text-white font-bold text-sm">A</div>
              <h1 className="font-bold text-slate-800 text-sm font-display tracking-tight">KOL Insight AI</h1>
            </div>

            <div className="text-center space-y-1.5">
              <h3 className="text-xl font-bold text-slate-800 font-display">欢迎登录系统</h3>
              <p className="text-xs text-slate-400">请选择下方安全快捷的登录方式访问管理后台</p>
            </div>

            {/* Custom high-contrast switcher tabs */}
            <div className="flex bg-slate-100 p-1 rounded-xl">
              <button
                onClick={() => setLoginMethod('sms')}
                className={`flex-1 flex items-center justify-center gap-2 py-2.5 text-xs font-bold rounded-lg transition-all duration-200 ${
                  loginMethod === 'sms' 
                    ? 'bg-white text-indigo-600 shadow-sm' 
                    : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                <Smartphone className="h-3.5 w-3.5" />
                手机快捷登录
              </button>
              <button
                onClick={() => setLoginMethod('wechat')}
                className={`flex-1 flex items-center justify-center gap-2 py-2.5 text-xs font-bold rounded-lg transition-all duration-200 ${
                  loginMethod === 'wechat' 
                    ? 'bg-white text-indigo-600 shadow-sm' 
                    : 'text-slate-500 hover:text-slate-800'
                }`}
              >
                <QrCode className="h-3.5 w-3.5" />
                微信扫码登录
              </button>
            </div>

            <AnimatePresence mode="wait">
              {loginMethod === 'sms' ? (
                // 1. SMS Verification form
                <motion.form
                  key="sms-form"
                  initial={{ opacity: 0, x: -15 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: 15 }}
                  transition={{ duration: 0.18 }}
                  onSubmit={handleSmsLogin}
                  className="space-y-4"
                >
                  {smsError && (
                    <div className="flex items-center gap-2 bg-rose-50 border border-rose-100 p-3 rounded-xl text-rose-600 text-xs font-medium">
                      <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                      <span>{smsError}</span>
                    </div>
                  )}

                  {smsSentNotice && (
                    <motion.div 
                      initial={{ scale: 0.95, opacity: 0 }}
                      animate={{ scale: 1, opacity: 1 }}
                      className="bg-emerald-50 border border-emerald-100 p-3.5 rounded-xl text-emerald-700 text-xs space-y-1"
                    >
                      <div className="flex items-center gap-1.5 font-bold">
                        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                        <span>【模拟网关】短信发送成功</span>
                      </div>
                      <p className="text-[11px] leading-normal opacity-90">
                        为了方便您测试，系统已自动填充接收到的验证码: <strong className="bg-emerald-100 px-1.5 py-0.5 rounded font-mono text-emerald-800 text-sm">{smsSentNotice}</strong>
                      </p>
                    </motion.div>
                  )}

                  {/* Phone input */}
                  <div className="space-y-1.5">
                    <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wide">
                      手机号码
                    </label>
                    <div className="relative">
                      <div className="absolute left-3 top-3 text-xs font-bold text-slate-400 border-r border-slate-200 pr-2">
                        +86
                      </div>
                      <input
                        type="tel"
                        maxLength={11}
                        placeholder="请输入11位中国手机号码"
                        value={phone}
                        onChange={e => {
                          setPhone(e.target.value.replace(/\D/g, ''));
                          setSmsError('');
                        }}
                        className="w-full bg-slate-50 border border-slate-200 rounded-xl py-2.5 pl-14 pr-3 text-xs focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-700 outline-none transition"
                        required
                      />
                    </div>
                  </div>

                  {/* Verification Code input */}
                  <div className="space-y-1.5">
                    <label className="text-[10px] font-bold text-slate-400 uppercase tracking-wide">
                      验证码
                    </label>
                    <div className="flex gap-2">
                      <div className="relative flex-1">
                        <input
                          type="text"
                          maxLength={6}
                          placeholder="请输入短信验证码"
                          value={code}
                          onChange={e => {
                            setCode(e.target.value.replace(/\D/g, ''));
                            setSmsError('');
                          }}
                          className="w-full bg-slate-50 border border-slate-200 rounded-xl py-2.5 pl-3 pr-3 text-xs focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 text-slate-700 outline-none transition font-mono tracking-wider"
                          required
                        />
                      </div>
                      <button
                        type="button"
                        disabled={countdown > 0}
                        onClick={handleSendCode}
                        className={`px-4 text-xs font-bold rounded-xl border shrink-0 transition ${
                          countdown > 0 
                            ? 'bg-slate-50 text-slate-400 border-slate-200 cursor-not-allowed' 
                            : 'bg-white hover:bg-indigo-50 text-indigo-600 border-indigo-200 hover:border-indigo-300 active:scale-95'
                        }`}
                      >
                        {countdown > 0 ? `${countdown}s 后重新发送` : '获取验证码'}
                      </button>
                    </div>
                  </div>

                  <button
                    type="submit"
                    disabled={isSmsSubmitting}
                    className="w-full mt-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-slate-300 text-white font-bold py-3 px-4 rounded-xl text-xs transition duration-150 shadow-md shadow-indigo-600/10 flex items-center justify-center gap-1.5 active:scale-[0.98]"
                  >
                    {isSmsSubmitting ? (
                      <>
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                        安全校验与加载中...
                      </>
                    ) : (
                      <>
                        立即安全登录
                        <ArrowRight className="h-3.5 w-3.5" />
                      </>
                    )}
                  </button>
                </motion.form>
              ) : (
                // 2. WeChat QR code scanner
                <motion.div
                  key="wechat-form"
                  initial={{ opacity: 0, x: 15 }}
                  animate={{ opacity: 1, x: 0 }}
                  exit={{ opacity: 0, x: -15 }}
                  transition={{ duration: 0.18 }}
                  className="flex flex-col items-center justify-center space-y-5 py-2"
                >
                  {/* Outer WeChat scanning board */}
                  <div className="relative p-4 bg-slate-50 rounded-2xl border border-slate-200/50 flex flex-col items-center justify-center w-52 h-52 group">
                    {/* Simulated QR code box */}
                    <div className="w-40 h-40 bg-white rounded-lg p-2.5 shadow-inner border border-slate-100 flex flex-col justify-between relative overflow-hidden">
                      
                      {/* WeChat QR code dots matrix simulation */}
                      <div className="w-full h-full flex flex-col gap-1 opacity-80 relative z-0">
                        {Array.from({ length: 11 }).map((_, rIdx) => (
                          <div key={rIdx} className="flex gap-1 justify-between">
                            {Array.from({ length: 11 }).map((_, cIdx) => {
                              const isCorner = 
                                (rIdx < 3 && cIdx < 3) || 
                                (rIdx < 3 && cIdx > 7) || 
                                (rIdx > 7 && cIdx < 3);
                              return (
                                <div 
                                  key={cIdx} 
                                  className={`w-2.5 h-2.5 rounded-xs shrink-0 ${
                                    isCorner 
                                      ? 'bg-slate-800 font-bold' 
                                      : Math.random() > 0.4 ? 'bg-indigo-600' : 'bg-slate-200'
                                  }`} 
                                />
                              );
                            })}
                          </div>
                        ))}
                      </div>

                      {/* WeChat Center Icon logo */}
                      <div className="absolute inset-0 m-auto w-10 h-10 bg-white border border-slate-100 rounded-lg flex items-center justify-center shadow z-10">
                        <MessageCircle className="h-5 w-5 text-emerald-500 fill-emerald-500" />
                      </div>

                      {/* Dynamic Scan status overlays */}
                      <AnimatePresence>
                        {wechatStatus === 'scanning' && (
                          <motion.div 
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            exit={{ opacity: 0 }}
                            className="absolute inset-0 bg-slate-900/65 backdrop-blur-xs flex flex-col items-center justify-center text-white p-2 text-center"
                          >
                            <Loader2 className="h-7 w-7 text-emerald-400 animate-spin mb-1.5" />
                            <span className="text-[10px] font-bold">已扫描笔记，等待确认...</span>
                          </motion.div>
                        )}

                        {wechatStatus === 'scanned' && (
                          <motion.div 
                            initial={{ opacity: 0 }}
                            animate={{ opacity: 1 }}
                            className="absolute inset-0 bg-emerald-600/90 flex flex-col items-center justify-center text-white p-2 text-center"
                          >
                            <CheckCircle2 className="h-8 w-8 text-white animate-bounce mb-1.5" />
                            <span className="text-[10px] font-bold">微信授权登录中...</span>
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>

                    {/* Laser line effect if scanning */}
                    {wechatStatus === 'idle' && (
                      <div className="absolute top-4 left-4 right-4 h-0.5 bg-indigo-500 shadow shadow-indigo-500 animate-bounce" />
                    )}
                  </div>

                  {/* Scanning instructions with nice feedback */}
                  <div className="text-center space-y-1.5">
                    <p className="text-xs font-bold text-slate-700 flex items-center justify-center gap-1">
                      {wechatStatus === 'idle' ? (
                        <>
                          <Loader2 className="h-3 w-3 text-indigo-500 animate-spin" />
                          <span>{wechatNotice}</span>
                        </>
                      ) : wechatStatus === 'scanning' ? (
                        <>
                          <Smartphone className="h-3.5 w-3.5 text-amber-500 animate-pulse" />
                          <span className="text-amber-500">{wechatNotice}</span>
                        </>
                      ) : (
                        <>
                          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                          <span className="text-emerald-600">{wechatNotice}</span>
                        </>
                      )}
                    </p>
                    <p className="text-[10px] text-slate-400">
                      安全多点验证 • 2分钟内有效 • 微信端授权一键同步
                    </p>
                  </div>

                  {/* Manual Fast Track simulator to avoid waiting */}
                  <button
                    onClick={handleInstantWechatLogin}
                    type="button"
                    className="px-3.5 py-1.5 text-[10px] font-semibold text-slate-500 bg-slate-100 hover:bg-slate-200 rounded-md transition duration-150 active:scale-95"
                  >
                    不想等待？点击此处一键模拟微信扫码成功
                  </button>
                </motion.div>
              )}
            </AnimatePresence>

            {/* Footer metadata compliance info */}
            <div className="pt-4 border-t border-slate-100/80 text-center flex flex-col items-center gap-1 text-[10px] text-slate-400">
              <span className="flex items-center gap-1 font-medium">
                <ShieldCheck className="h-3.5 w-3.5 text-indigo-500" />
                国家等级保护安全系统、SSL链路级别加密支持
              </span>
              <p>登录即代表您已同意《KOL Insight AI 用户服务协议》与《隐私保护指引》</p>
            </div>
          </motion.div>
        </div>

      </div>
    </div>
  );
}
