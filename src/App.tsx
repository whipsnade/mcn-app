import React, { useState, useEffect } from 'react';
import { initialSessions } from './initialData';
import { Session, Message, ReportData } from './types';
import SessionList from './components/SessionList';
import ChatArea from './components/ChatArea';
import BiReport from './components/BiReport';
import NewSessionModal from './components/NewSessionModal';

export default function App() {
  const [sessions, setSessions] = useState<Session[]>(() => {
    const saved = localStorage.getItem('kol_mcn_analyst_sessions');
    return saved ? JSON.parse(saved) : initialSessions;
  });
  
  const [activeSessionId, setActiveSessionId] = useState<string>(() => {
    const savedActive = localStorage.getItem('kol_mcn_analyst_active_id');
    return savedActive || 'WO-1001';
  });

  const [isNewModalOpen, setIsNewModalOpen] = useState(false);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [isMockMode, setIsMockMode] = useState(false);

  // Sync to local storage on changes
  useEffect(() => {
    localStorage.setItem('kol_mcn_analyst_sessions', JSON.stringify(sessions));
  }, [sessions]);

  useEffect(() => {
    localStorage.setItem('kol_mcn_analyst_active_id', activeSessionId);
  }, [activeSessionId]);

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

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-slate-100 antialiased text-slate-900 font-sans">
      
      {/* 1. Left panel: Active Conversations List */}
      <SessionList
        sessions={sessions}
        activeSessionId={activeSessionId}
        onSelectSession={setActiveSessionId}
        onOpenNewModal={() => setIsNewModalOpen(true)}
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
      />

      {/* New Session Creation Popup Modal */}
      <NewSessionModal
        isOpen={isNewModalOpen}
        onClose={() => setIsNewModalOpen(false)}
        onCreate={handleCreateSession}
      />

    </div>
  );
}
