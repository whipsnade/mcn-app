import React, { useState, useEffect } from 'react';
import { initialSessions } from './initialData';
import { Session, Message, ReportData, Account } from './types';
import SessionList from './components/SessionList';
import ChatArea from './components/ChatArea';
import BiReport from './components/BiReport';
import NewSessionModal from './components/NewSessionModal';
import LoginPage from './components/LoginPage';
import RechargeModal from './components/RechargeModal';
import AdminPanel from './components/AdminPanel';

export default function App() {
  const [user, setUser] = useState<{ phone?: string; loginMethod: 'sms' | 'wechat'; nickname: string } | null>(() => {
    const saved = localStorage.getItem('kol_mcn_analyst_user');
    return saved ? JSON.parse(saved) : null;
  });

  const [sessions, setSessions] = useState<Session[]>(() => {
    const saved = localStorage.getItem('kol_mcn_analyst_sessions');
    return saved ? JSON.parse(saved) : initialSessions;
  });
  
  const [activeSessionId, setActiveSessionId] = useState<string>(() => {
    const savedActive = localStorage.getItem('kol_mcn_analyst_active_id');
    return savedActive || 'WO-1001';
  });

  const [points, setPoints] = useState<number>(() => {
    const saved = localStorage.getItem('kol_analyst_points');
    return saved ? parseInt(saved, 10) : 3450;
  });

  const [accounts, setAccounts] = useState<Account[]>(() => {
    const saved = localStorage.getItem('kol_mcn_analyst_accounts');
    if (saved) return JSON.parse(saved);
    return [
      {
        id: 'acc-1',
        username: '系统超级管理员',
        phone: '18888888888',
        channels: ["小红书", "抖音", "B站", "微博", "YouTube", "Instagram"],
        points: 5000,
        role: 'admin',
        createdAt: '2026-01-01'
      },
      {
        id: 'acc-2',
        username: '手机用户_Anker',
        phone: '13812345678',
        channels: ["小红书", "抖音", "B站"],
        points: 3450,
        role: 'user',
        createdAt: '2026-06-15'
      },
      {
        id: 'acc-3',
        username: '微信快捷登录用户',
        phone: '13900001111',
        channels: ["小红书", "微博"],
        points: 1200,
        role: 'user',
        createdAt: '2026-07-01'
      }
    ];
  });

  const [isRechargeOpen, setIsRechargeOpen] = useState(false);
  const [isAdminOpen, setIsAdminOpen] = useState(false);

  const [isNewModalOpen, setIsNewModalOpen] = useState(false);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isMockMode, setIsMockMode] = useState(false);

  // Sync user state to localStorage
  useEffect(() => {
    if (user) {
      localStorage.setItem('kol_mcn_analyst_user', JSON.stringify(user));
    } else {
      localStorage.removeItem('kol_mcn_analyst_user');
    }
  }, [user]);

  // Sync accounts to localStorage
  useEffect(() => {
    localStorage.setItem('kol_mcn_analyst_accounts', JSON.stringify(accounts));
  }, [accounts]);

  // Sync points from current user account changes to local points state
  useEffect(() => {
    if (user) {
      const matchPhone = user.phone || '';
      const matchNick = user.nickname || '';
      const matched = accounts.find(a => 
        (matchPhone && a.phone === matchPhone) || 
        (matchNick && a.username === matchNick)
      );
      if (matched && matched.points !== points) {
        setPoints(matched.points);
      }
    }
  }, [accounts, user]);

  // Sync local points changes back to matching account
  useEffect(() => {
    if (user) {
      const matchPhone = user.phone || '';
      const matchNick = user.nickname || '';
      const matched = accounts.find(a => 
        (matchPhone && a.phone === matchPhone) || 
        (matchNick && a.username === matchNick)
      );
      if (matched && matched.points !== points) {
        setAccounts(prev => prev.map(a => 
          a.id === matched.id ? { ...a, points } : a
        ));
      }
    }
  }, [points]);

  
  // Sync to local storage on changes
  useEffect(() => {
    localStorage.setItem('kol_mcn_analyst_sessions', JSON.stringify(sessions));
  }, [sessions]);

  useEffect(() => {
    localStorage.setItem('kol_mcn_analyst_active_id', activeSessionId);
  }, [activeSessionId]);

  useEffect(() => {
    localStorage.setItem('kol_analyst_points', points.toString());
  }, [points]);

  const handleLogout = () => {
    setUser(null);
  };

  // Find currently active session
  const activeSession = sessions.find(s => s.id === activeSessionId) || sessions[0];

  // Handle message sending to AI agent & updating BI report on-the-fly
  const handleSendMessage = async (text: string) => {
    if (!text.trim() || isAnalyzing) return;

    const userMessage: Message = {
      id: `m-user-${Date.now()}`,
      sender: 'user',
      text: text.trim(),
      timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    };

    // Update session state with the user message immediately
    const updatedMessages = [...activeSession.messages, userMessage];
    setSessions(prev => prev.map(s => 
      s.id === activeSession.id ? { ...s, messages: updatedMessages } : s
    ));
    setIsAnalyzing(true);

    try {
      const response = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: updatedMessages,
          brand: activeSession.brand,
          campaignName: activeSession.campaignName,
          platform: activeSession.platform,
          mcn: activeSession.mcn,
          kols: activeSession.kols,
          currentReportData: activeSession.reportData
        })
      });

      if (!response.ok) {
        throw new Error(`Server returned code ${response.status}`);
      }

      const result = await response.json();

      const aiMessage: Message = {
        id: `m-ai-${Date.now()}`,
        sender: 'ai',
        text: result.reply,
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      };

      // Set Mock indicator
      setIsMockMode(!!result.isMock);

      // Update session with AI message and newly calculated BI reportData!
      setSessions(prev => prev.map(s => 
        s.id === activeSession.id ? {
          ...s,
          messages: [...updatedMessages, aiMessage],
          reportData: result.reportData || s.reportData
        } : s
      ));

    } catch (err) {
      console.error("Analysis API failed:", err);
      
      const systemErrorMsg: Message = {
        id: `m-sys-${Date.now()}`,
        sender: 'system',
        text: "❌ 远程AI服务连接发生网络扰动。已触发本地应急数据清洗机制保护会话稳定性。",
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      };

      setSessions(prev => prev.map(s => 
        s.id === activeSession.id ? {
          ...s,
          messages: [...updatedMessages, systemErrorMsg]
        } : s
      ));
    } finally {
      setIsAnalyzing(false);
    }
  };

  // Handle creation of a brand new Campaign Session
  const handleCreateSession = async (data: {
    brand: string;
    campaignName: string;
    platform: string;
    mcn: string;
    kols: string[];
    initialQuery: string;
  }) => {
    const newSessionId = `WO-${1001 + sessions.length}`;
    
    const newSession: Session = {
      id: newSessionId,
      title: `${data.brand}-${data.campaignName}`,
      brand: data.brand,
      campaignName: data.campaignName,
      platform: data.platform,
      mcn: data.mcn,
      kols: data.kols,
      status: 'analyzing',
      summary: data.initialQuery,
      messages: [
        {
          id: `init-${Date.now()}`,
          sender: 'user',
          text: data.initialQuery,
          timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
        }
      ]
    };

    // Pre-add session and select it
    setSessions(prev => [...prev, newSession]);
    setActiveSessionId(newSessionId);
    setIsAnalyzing(true);

    try {
      // Fetch initial structured report from AI for this specific custom campaign
      const response = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: newSession.messages,
          brand: data.brand,
          campaignName: data.campaignName,
          platform: data.platform,
          mcn: data.mcn,
          kols: data.kols,
          currentReportData: null
        })
      });

      if (!response.ok) throw new Error("API init call failed");
      const result = await response.json();

      const aiResponseMsg: Message = {
        id: `ai-init-${Date.now()}`,
        sender: 'ai',
        text: result.reply,
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      };

      setIsMockMode(!!result.isMock);

      setSessions(prev => prev.map(s => 
        s.id === newSessionId ? {
          ...s,
          status: 'completed',
          messages: [...s.messages, aiResponseMsg],
          reportData: result.reportData
        } : s
      ));

    } catch (err) {
      console.error("Initial analysis generation failed:", err);
      
      const systemErrorMsg: Message = {
        id: `sys-init-${Date.now()}`,
        sender: 'system',
        text: "⚠️ 远程智能大模型构建首版报告时发生延时。请输入更多对话指令唤醒重算机制。",
        timestamp: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
      };

      setSessions(prev => prev.map(s => 
        s.id === newSessionId ? {
          ...s,
          messages: [...s.messages, systemErrorMsg]
        } : s
      ));
    } finally {
      setIsAnalyzing(false);
    }
  };

  const handleToggleStar = (id: string) => {
    setSessions(prev => prev.map(s => 
      s.id === id ? { ...s, isStarred: !s.isStarred } : s
    ));
  };

  const handleRenameSession = (id: string, newBrand: string, newCampaignName: string) => {
    setSessions(prev => prev.map(s => 
      s.id === id ? { 
        ...s, 
        brand: newBrand, 
        campaignName: newCampaignName,
        title: `${newBrand}-${newCampaignName}`
      } : s
    ));
  };

  const handleLoginSuccess = (userInfo: { phone?: string; loginMethod: 'sms' | 'wechat'; nickname: string }) => {
    const matched = accounts.find(a => 
      (userInfo.phone && a.phone === userInfo.phone) || 
      (userInfo.nickname && a.username === userInfo.nickname)
    );

    if (matched) {
      setUser({
        phone: matched.phone,
        loginMethod: userInfo.loginMethod,
        nickname: matched.username
      });
      setPoints(matched.points);
    } else {
      const newPhone = userInfo.phone || `135${Math.floor(10000000 + Math.random() * 90000000)}`;
      const newAcc: Account = {
        id: `acc-${Date.now()}`,
        username: userInfo.nickname,
        phone: newPhone,
        channels: ["小红书", "抖音", "B站"],
        points: 2000,
        role: 'user',
        createdAt: new Date().toLocaleDateString('zh-CN')
      };
      setAccounts(prev => [...prev, newAcc]);
      setUser({
        phone: newAcc.phone,
        loginMethod: userInfo.loginMethod,
        nickname: newAcc.username
      });
      setPoints(newAcc.points);
    }
  };

  if (!user) {
    return <LoginPage onLoginSuccess={handleLoginSuccess} />;
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-slate-100 antialiased text-slate-900 font-sans">
      
      {/* 1. Left panel: Active Conversations List */}
      <SessionList
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={setActiveSessionId}
        onOpenNewModal={() => setIsNewModalOpen(true)}
        onToggleStar={handleToggleStar}
        onRenameSession={handleRenameSession}
        user={user}
        onLogout={handleLogout}
        points={points}
        onOpenRecharge={() => setIsRechargeOpen(true)}
        onOpenAdmin={() => setIsAdminOpen(true)}
      />

      {/* 2. Middle panel: Live AI Analyst Conversation */}
      {activeSession ? (
        <ChatArea
          session={activeSession}
          onSendMessage={handleSendMessage}
          isAnalyzing={isAnalyzing}
          isMockMode={isMockMode}
        />
      ) : (
        <div className="flex-1 flex items-center justify-center bg-slate-50">
          <p className="text-xs text-slate-400 font-medium">请选择或新建一个营销会话</p>
        </div>
      )}

      {/* 3. Right panel: Live BI Analytical Dashboard Report */}
      <BiReport
        reportData={activeSession?.reportData}
        campaignName={activeSession?.campaignName || ''}
        brand={activeSession?.brand || ''}
        sessions={sessions}
      />

      {/* New Session Creation Popup Modal */}
      <NewSessionModal
        isOpen={isNewModalOpen}
        onClose={() => setIsNewModalOpen(false)}
        onCreate={handleCreateSession}
      />

      {/* Recharge Modal */}
      <RechargeModal
        isOpen={isRechargeOpen}
        onClose={() => setIsRechargeOpen(false)}
        onRechargeSuccess={(added) => {
          setPoints(prev => Math.min(prev + added, 5000));
          setIsRechargeOpen(false);
        }}
        currentPoints={points}
        maxPoints={5000}
      />

      {/* Admin Panel Console */}
      <AdminPanel
        isOpen={isAdminOpen}
        onClose={() => setIsAdminOpen(false)}
        accounts={accounts}
        onUpdateAccounts={setAccounts}
        currentUserPhone={user?.phone}
        currentUserNickname={user?.nickname}
      />

    </div>
  );
}
