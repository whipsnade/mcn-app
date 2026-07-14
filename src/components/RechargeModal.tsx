import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'motion/react';
import { 
  X, 
  CheckCircle2, 
  Loader2, 
  Sparkles, 
  ShieldCheck, 
  Zap, 
  QrCode, 
  Smartphone,
  CreditCard
} from 'lucide-react';

interface RechargeModalProps {
  isOpen: boolean;
  onClose: () => void;
  onRechargeSuccess: (pointsToAdd: number) => void;
  currentPoints: number;
  maxPoints: number;
}

interface PricingPackage {
  id: string;
  name: string;
  points: number;
  bonus: number;
  price: number;
  tag?: string;
  desc: string;
}

const PACKAGES: PricingPackage[] = [
  {
    id: 'trial',
    name: '体验版额度',
    points: 500,
    bonus: 0,
    price: 9.9,
    desc: '适用于轻度舆情查询与单次KOL分析'
  },
  {
    id: 'pro',
    name: '专业版额度',
    points: 2000,
    bonus: 200,
    price: 29.9,
    tag: '超值推荐',
    desc: '包含多平台受众画像与多轮竞品比对'
  },
  {
    id: 'enterprise',
    name: '企业版精选',
    points: 5000,
    bonus: 800,
    price: 59.9,
    tag: '加赠16%',
    desc: '大项目专享，附带MCN预算ROI分析高阶报表'
  }
];

export default function RechargeModal({
  isOpen,
  onClose,
  onRechargeSuccess,
  currentPoints,
  maxPoints
}: RechargeModalProps) {
  const [selectedPkg, setSelectedPkg] = useState<PricingPackage>(PACKAGES[1]);
  const [payMethod, setPayMethod] = useState<'wechat' | 'alipay'>('wechat');
  const [payStatus, setPayStatus] = useState<'idle' | 'scanning' | 'scanned' | 'success'>('idle');
  const [customAmountActive, setCustomAmountActive] = useState(false);
  const [customPointsInput, setCustomPointsInput] = useState('1000');
  const [secondsRemaining, setSecondsRemaining] = useState(120);

  // QR code validity countdown
  useEffect(() => {
    if (!isOpen) return;
    setSecondsRemaining(120);
    const timer = setInterval(() => {
      setSecondsRemaining(prev => {
        if (prev <= 1) {
          clearInterval(timer);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [isOpen, selectedPkg, payMethod, customAmountActive, customPointsInput]);

  // Handle simulated auto-scanning transitions on package/payment change
  useEffect(() => {
    if (!isOpen) return;
    setPayStatus('idle');
    
    // Simulate payment state progression:
    // 1. Idle for 3 seconds
    // 2. User/Simulator scans (wait 3s)
    // 3. User approves (wait 2s) -> success!
    const scanTimeout = setTimeout(() => {
      setPayStatus('scanning');
      const confirmTimeout = setTimeout(() => {
        setPayStatus('scanned');
        const successTimeout = setTimeout(() => {
          setPayStatus('success');
          // Add points
          const added = customAmountActive 
            ? (parseInt(customPointsInput, 10) || 0) 
            : (selectedPkg.points + selectedPkg.bonus);
          
          setTimeout(() => {
            onRechargeSuccess(added);
          }, 1000);
        }, 1500);
        return () => clearTimeout(successTimeout);
      }, 2500);
      return () => clearTimeout(confirmTimeout);
    }, 3500);

    return () => {
      clearTimeout(scanTimeout);
    };
  }, [isOpen, selectedPkg, payMethod, customAmountActive, customPointsInput]);

  if (!isOpen) return null;

  // Custom calculations
  const customPointsVal = Math.max(100, parseInt(customPointsInput, 10) || 0);
  const customPrice = parseFloat((customPointsVal * 0.015).toFixed(1)); // 0.015 CNY per point

  const displayPoints = customAmountActive ? customPointsVal : (selectedPkg.points + selectedPkg.bonus);
  const displayBasePoints = customAmountActive ? customPointsVal : selectedPkg.points;
  const displayBonus = customAmountActive ? 0 : selectedPkg.bonus;
  const displayPrice = customAmountActive ? customPrice : selectedPkg.price;

  const handleInstantPay = () => {
    setPayStatus('success');
    const added = customAmountActive ? customPointsVal : (selectedPkg.points + selectedPkg.bonus);
    setTimeout(() => {
      onRechargeSuccess(added);
    }, 1200);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/40 backdrop-blur-xs font-sans">
      <motion.div
        initial={{ opacity: 0, scale: 0.95, y: 15 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 15 }}
        className="relative w-full max-w-3xl bg-white rounded-2xl border border-slate-100 shadow-2xl flex flex-col md:flex-row overflow-hidden"
      >
        {/* Left Panel: Package Selector */}
        <div className="flex-1 p-6 md:p-8 space-y-6 bg-white">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="h-7 w-7 rounded-lg bg-indigo-100 flex items-center justify-center text-indigo-600">
                <Zap className="h-4 w-4 fill-indigo-600/10" />
              </div>
              <div>
                <h3 className="text-base font-bold text-slate-800 font-display">极客决策中心 • 积分充值</h3>
                <p className="text-[10px] text-slate-400">实时充值即刻到账，尊享高精准度智能研判</p>
              </div>
            </div>
            {/* Close button for mobile */}
            <button 
              onClick={onClose} 
              className="md:hidden p-1.5 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          {/* Current balance indicator */}
          <div className="p-3 bg-slate-50 border border-slate-100 rounded-xl flex items-center justify-between text-xs">
            <div className="space-y-0.5">
              <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wide">当前可用额度</span>
              <p className="font-bold text-slate-700 font-display">{currentPoints.toLocaleString()} / {maxPoints.toLocaleString()} 点</p>
            </div>
            <span className="text-[10px] bg-indigo-50 text-indigo-600 font-bold px-2 py-0.5 rounded-md">
              已用 {(((maxPoints - currentPoints) / maxPoints) * 100).toFixed(0)}%
            </span>
          </div>

          {/* Packages selection grids */}
          <div className="space-y-3">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wide block">选择额度套餐</span>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              {PACKAGES.map((pkg) => {
                const isSelected = !customAmountActive && selectedPkg.id === pkg.id;
                return (
                  <button
                    key={pkg.id}
                    onClick={() => {
                      setCustomAmountActive(false);
                      setSelectedPkg(pkg);
                    }}
                    type="button"
                    className={`relative p-3.5 rounded-xl border text-left transition flex flex-col justify-between h-32 hover:border-indigo-400 ${
                      isSelected
                        ? 'border-indigo-600 bg-indigo-50/20 ring-2 ring-indigo-500/10'
                        : 'border-slate-200 bg-white'
                    }`}
                  >
                    {pkg.tag && (
                      <span className="absolute top-2 right-2 text-[8px] font-extrabold px-1.5 py-0.5 rounded-md bg-rose-500 text-white shadow-xs">
                        {pkg.tag}
                      </span>
                    )}
                    <div className="space-y-1">
                      <p className={`text-xs font-bold ${isSelected ? 'text-indigo-600' : 'text-slate-700'}`}>{pkg.name}</p>
                      <p className="text-[10px] text-slate-400 leading-normal line-clamp-2">{pkg.desc}</p>
                    </div>
                    <div className="pt-2">
                      <p className="text-sm font-bold text-slate-800 font-display">
                        {(pkg.points + pkg.bonus).toLocaleString()} <span className="text-[10px] font-medium text-slate-400">点</span>
                      </p>
                      <p className="text-[11px] font-extrabold text-indigo-600 font-display">
                        ¥ {pkg.price}
                      </p>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          {/* Custom Amount option */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wide">自定义充值点数</span>
              <button
                type="button"
                onClick={() => setCustomAmountActive(!customAmountActive)}
                className={`text-[10px] font-bold px-2 py-0.5 rounded transition ${
                  customAmountActive ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-500 hover:bg-slate-200'
                }`}
              >
                {customAmountActive ? "取消自定义" : "切换自定义"}
              </button>
            </div>
            
            <AnimatePresence>
              {customAmountActive && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: 'auto' }}
                  exit={{ opacity: 0, height: 0 }}
                  className="overflow-hidden"
                >
                  <div className="p-3 bg-slate-50 border border-slate-200 rounded-xl space-y-2 flex items-center justify-between gap-4">
                    <div className="flex-1 space-y-1">
                      <label className="text-[9px] font-bold text-slate-400 block">输入所需额度点数 (最低 100 点)</label>
                      <input
                        type="number"
                        min="100"
                        step="100"
                        value={customPointsInput}
                        onChange={e => {
                          const val = e.target.value.replace(/\D/g, '');
                          setCustomPointsInput(val);
                        }}
                        className="w-full bg-white border border-slate-200 rounded-lg px-3 py-1.5 text-xs text-slate-700 outline-none focus:border-indigo-500 font-mono tracking-wider font-semibold"
                        placeholder="请输入点数，如 1000"
                      />
                    </div>
                    <div className="text-right shrink-0 space-y-1">
                      <span className="text-[9px] font-bold text-slate-400 block">实付金额估计</span>
                      <p className="text-lg font-bold text-indigo-600 font-display">
                        ¥ {customPrice}
                      </p>
                    </div>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </div>

          {/* Payment Method Option */}
          <div className="space-y-2">
            <span className="text-[10px] font-bold text-slate-400 uppercase tracking-wide block">选择支付方式</span>
            <div className="grid grid-cols-2 gap-3">
              <button
                type="button"
                onClick={() => setPayMethod('wechat')}
                className={`flex items-center justify-center gap-2 py-2.5 rounded-xl border text-xs font-bold transition active:scale-95 ${
                  payMethod === 'wechat'
                    ? 'border-emerald-500 bg-emerald-50/10 text-emerald-600'
                    : 'border-slate-200 text-slate-500 hover:bg-slate-50'
                }`}
              >
                <Smartphone className="h-4 w-4 text-emerald-500" />
                微信支付
              </button>
              <button
                type="button"
                onClick={() => setPayMethod('alipay')}
                className={`flex items-center justify-center gap-2 py-2.5 rounded-xl border text-xs font-bold transition active:scale-95 ${
                  payMethod === 'alipay'
                    ? 'border-sky-500 bg-sky-50/10 text-sky-600'
                    : 'border-slate-200 text-slate-500 hover:bg-slate-50'
                }`}
              >
                <CreditCard className="h-4 w-4 text-sky-500" />
                支付宝
              </button>
            </div>
          </div>
        </div>

        {/* Right Panel: Simulated QR Scanning Screen */}
        <div className="w-full md:w-72 bg-slate-50 border-t md:border-t-0 md:border-l border-slate-100 p-6 md:p-8 flex flex-col justify-between items-center relative overflow-hidden">
          {/* Top close button for desktop */}
          <button 
            onClick={onClose} 
            className="hidden md:block absolute top-4 right-4 p-1.5 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-200 transition"
            title="关闭页面"
          >
            <X className="h-4 w-4" />
          </button>

          {/* Ticket detail */}
          <div className="text-center space-y-1.5 w-full mt-2">
            <span className="text-[10px] font-extrabold text-slate-400 uppercase tracking-wider block">应付总额</span>
            <p className="text-2xl font-bold text-slate-800 font-display">
              ¥ {displayPrice}
            </p>
            <div className="text-[10px] text-indigo-600 bg-indigo-50/50 inline-block px-2 py-0.5 rounded-md font-semibold">
              充值 {displayPoints.toLocaleString()} 积分 
              {displayBonus > 0 && <span className="text-emerald-600"> (含赠送 {displayBonus} 点)</span>}
            </div>
          </div>

          {/* QR Code Container */}
          <div className="my-6 relative flex flex-col items-center">
            <div className="p-3 bg-white rounded-xl border border-slate-200/50 flex flex-col items-center justify-center w-40 h-40 relative group shadow-sm">
              {/* Simulated matrix dots */}
              <div className="w-full h-full flex flex-col gap-1 opacity-85 select-none">
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
                          className={`w-2 h-2 rounded-xs shrink-0 ${
                            isCorner 
                              ? (payMethod === 'wechat' ? 'bg-emerald-600 font-bold' : 'bg-sky-600 font-bold')
                              : Math.random() > 0.45 
                                ? (payMethod === 'wechat' ? 'bg-emerald-500' : 'bg-sky-500') 
                                : 'bg-slate-100'
                          }`} 
                        />
                      );
                    })}
                  </div>
                ))}
              </div>

              {/* Central Logo */}
              <div className="absolute inset-0 m-auto w-9 h-9 bg-white border border-slate-100 rounded-lg flex items-center justify-center shadow-xs">
                {payMethod === 'wechat' ? (
                  <QrCode className="h-5 w-5 text-emerald-500" />
                ) : (
                  <QrCode className="h-5 w-5 text-sky-500" />
                )}
              </div>

              {/* Scanned overlays */}
              <AnimatePresence>
                {payStatus === 'scanning' && (
                  <motion.div 
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    className="absolute inset-0 bg-slate-900/65 backdrop-blur-xs flex flex-col items-center justify-center text-white p-2 text-center rounded-xl"
                  >
                    <Loader2 className="h-6 w-6 text-indigo-400 animate-spin mb-1" />
                    <span className="text-[10px] font-bold">已扫描，请在手机端确认</span>
                  </motion.div>
                )}

                {payStatus === 'scanned' && (
                  <motion.div 
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    className="absolute inset-0 bg-emerald-600/90 flex flex-col items-center justify-center text-white p-2 text-center rounded-xl"
                  >
                    <CheckCircle2 className="h-7 w-7 text-white animate-bounce mb-1" />
                    <span className="text-[10px] font-bold">支付授权中...</span>
                  </motion.div>
                )}

                {payStatus === 'success' && (
                  <motion.div 
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    className="absolute inset-0 bg-emerald-600 flex flex-col items-center justify-center text-white p-2 text-center rounded-xl"
                  >
                    <CheckCircle2 className="h-8 w-8 text-white mb-1.5" />
                    <span className="text-[11px] font-bold">充值成功!</span>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>

            {/* Simulated bouncing laser bar */}
            {payStatus === 'idle' && (
              <div className="absolute top-3 left-3 right-3 h-0.5 bg-indigo-500 shadow-sm animate-bounce" />
            )}
          </div>

          {/* Instructions */}
          <div className="text-center space-y-1.5 w-full">
            <div className="text-[11px] font-bold text-slate-700 flex items-center justify-center gap-1">
              {payStatus === 'idle' ? (
                <>
                  <QrCode className="h-3 w-3 text-indigo-500 animate-pulse" />
                  <span>使用 {payMethod === 'wechat' ? '微信' : '支付宝'} 扫一扫</span>
                </>
              ) : payStatus === 'scanning' ? (
                <>
                  <Loader2 className="h-3 w-3 text-amber-500 animate-spin" />
                  <span className="text-amber-500">已检测到扫描，等待授权</span>
                </>
              ) : payStatus === 'scanned' ? (
                <>
                  <CheckCircle2 className="h-3 w-3 text-emerald-500" />
                  <span className="text-emerald-600">微信/支付宝端已完成确认</span>
                </>
              ) : (
                <>
                  <CheckCircle2 className="h-3 w-3 text-emerald-500 animate-pulse" />
                  <span className="text-emerald-600">已到账，关闭页面中...</span>
                </>
              )}
            </div>
            
            <p className="text-[9px] text-slate-400 font-medium">
              二维码有效时间: <span className="font-mono text-indigo-600 font-bold">{secondsRemaining}</span> 秒
            </p>

            {/* Instant checkout simulator button to save users time */}
            <button
              onClick={handleInstantPay}
              type="button"
              className="mt-2 text-[9px] font-bold text-slate-400 bg-slate-100 hover:bg-slate-200 px-3 py-1 rounded-md transition duration-150 active:scale-95 border border-slate-200/50"
            >
              🚀 不想等待？点击一键模拟扫码成功
            </button>
          </div>

          {/* Secure compliance notice */}
          <div className="mt-4 pt-3 border-t border-slate-200/50 text-center w-full flex items-center justify-center gap-1 text-[8px] text-slate-400">
            <ShieldCheck className="h-3 w-3 text-indigo-500 shrink-0" />
            <span>SSL加密链路保护，账户信息受到严格加密</span>
          </div>

        </div>
      </motion.div>
    </div>
  );
}
