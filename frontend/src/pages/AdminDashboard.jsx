import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { toast } from 'react-hot-toast';
import {
  LogIn,
  LogOut,
  Users,
  Key,
  Trash2,
  CheckCircle,
  XCircle,
  Shield,
  Plus,
  TrendingUp,
  Activity,
  Calendar,
  Eye,
  Download,
  RefreshCw,
  Settings,
  Save,
  BarChart3,
  Database,
  Wallet,
  Clock,
  FileText,
  Loader2
} from 'lucide-react';
import ConfigManager from '../components/ConfigManager';
import SessionMonitor from '../components/SessionMonitor';
import DatabaseManager from '../components/DatabaseManager';
import { formatChinaDateForFilename, formatChinaDateTime } from '../utils/timezone';

const AdminDashboard = () => {
  const navigate = useNavigate();
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [loading, setLoading] = useState(false);
  const [adminToken, setAdminToken] = useState(localStorage.getItem('adminToken'));
  
  // Tab state
  const [activeTab, setActiveTab] = useState('dashboard');

  // Login form state
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  // Users state
  const [users, setUsers] = useState([]);
  const [loadingUsers, setLoadingUsers] = useState(false);

  // Statistics state
  const [statistics, setStatistics] = useState(null);
  const [loadingStats, setLoadingStats] = useState(false);

  // Card key generation state
  const [newCardKey, setNewCardKey] = useState('');
  const [newCardInitialBalanceYuan, setNewCardInitialBalanceYuan] = useState('');
  const [generatedKey, setGeneratedKey] = useState('');
  
  // Batch generation state
  const [batchCount, setBatchCount] = useState(5);
  const [batchPrefix, setBatchPrefix] = useState('');
  const [batchInitialBalanceYuan, setBatchInitialBalanceYuan] = useState('');
  const [showBatchModal, setShowBatchModal] = useState(false);

  // Billing config state
  const [billingPriceYuan, setBillingPriceYuan] = useState('');
  const [loadingBillingConfig, setLoadingBillingConfig] = useState(false);
  const [savingBillingConfig, setSavingBillingConfig] = useState(false);
  
  // User details modal
  const [selectedUser, setSelectedUser] = useState(null);
  const [userDetails, setUserDetails] = useState(null);
  const [showUserDetails, setShowUserDetails] = useState(false);

  // Balance adjustment modal
  const [balanceUser, setBalanceUser] = useState(null);
  const [balanceAmountYuan, setBalanceAmountYuan] = useState('');
  const [balanceReason, setBalanceReason] = useState('');
  const [adjustingBalance, setAdjustingBalance] = useState(false);

  useEffect(() => {
    if (adminToken) {
      verifyToken();
    }
  }, [adminToken]);

  useEffect(() => {
    if (isAuthenticated) {
      fetchStatistics();
      // 每30秒自动刷新统计数据
      const interval = setInterval(fetchStatistics, 30000);
      return () => clearInterval(interval);
    }
  }, [isAuthenticated]);

  useEffect(() => {
    if (isAuthenticated && activeTab === 'billing') {
      fetchBillingConfig();
    }
  }, [isAuthenticated, activeTab]);

  const verifyToken = async () => {
    try {
      await axios.post('/api/admin/verify-token', {}, {
        headers: { Authorization: `Bearer ${adminToken}` }
      });
      setIsAuthenticated(true);
      fetchUsers();
    } catch (error) {
      localStorage.removeItem('adminToken');
      setAdminToken(null);
      setIsAuthenticated(false);
    }
  };

  const yuanToCents = (value) => {
    const amount = Number(value);
    if (!Number.isFinite(amount)) {
      return 0;
    }
    return Math.round(amount * 100);
  };

  const formatCents = (cents) => `¥${(Number(cents || 0) / 100).toFixed(2)}`;

  const handleLogin = async (e) => {
    e.preventDefault();
    setLoading(true);

    try {
      const response = await axios.post('/api/admin/login', {
        username,
        password
      });

      const { access_token } = response.data;
      localStorage.setItem('adminToken', access_token);
      setAdminToken(access_token);
      setIsAuthenticated(true);
      toast.success('登录成功！');
      fetchUsers(access_token);
    } catch (error) {
      toast.error(error.response?.data?.detail || '登录失败，请检查用户名和密码');
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('adminToken');
    setAdminToken(null);
    setIsAuthenticated(false);
    setUsername('');
    setPassword('');
    toast.success('已退出登录');
  };

  const fetchUsers = async (tokenOverride = adminToken) => {
    setLoadingUsers(true);
    try {
      const response = await axios.get('/api/admin/users', {
        headers: { Authorization: `Bearer ${tokenOverride}` }
      });
      setUsers(response.data);
    } catch (error) {
      toast.error('获取用户列表失败');
      console.error('Error fetching users:', error);
    } finally {
      setLoadingUsers(false);
    }
  };

  const fetchStatistics = async () => {
    setLoadingStats(true);
    try {
      const response = await axios.get('/api/admin/statistics', {
        headers: { Authorization: `Bearer ${adminToken}` }
      });
      setStatistics(response.data);
    } catch (error) {
      console.error('Error fetching statistics:', error);
    } finally {
      setLoadingStats(false);
    }
  };

  const fetchBillingConfig = async () => {
    setLoadingBillingConfig(true);
    try {
      const response = await axios.get('/api/admin/config', {
        headers: { Authorization: `Bearer ${adminToken}` }
      });
      const cents = Number(response.data.system?.workspace_price_per_10k_cents || 0);
      setBillingPriceYuan(cents > 0 ? (cents / 100).toString() : '');
    } catch (error) {
      toast.error('获取计费配置失败');
    } finally {
      setLoadingBillingConfig(false);
    }
  };

  const handleSaveBillingPrice = async () => {
    const price = Number(billingPriceYuan);
    if (!Number.isFinite(price) || price < 0) {
      toast.error('请输入有效的每万字价格');
      return;
    }

    setSavingBillingConfig(true);
    try {
      await axios.post(
        '/api/admin/config',
        { WORKSPACE_PRICE_PER_10K_CENTS: Math.round(price * 100).toString() },
        { headers: { Authorization: `Bearer ${adminToken}` } }
      );
      toast.success('计费价格已保存');
      fetchBillingConfig();
    } catch (error) {
      toast.error(error.response?.data?.detail || '保存计费价格失败');
    } finally {
      setSavingBillingConfig(false);
    }
  };

  const handleGenerateCardKey = async (e) => {
    e.preventDefault();

    try {
      const initialBalanceCents = yuanToCents(newCardInitialBalanceYuan);
      const response = await axios.post('/api/admin/card-keys', 
        {
          card_key: newCardKey.trim() || undefined,
          initial_balance_cents: Math.max(0, initialBalanceCents)
        },
        { headers: { Authorization: `Bearer ${adminToken}` } }
      );
      
      setGeneratedKey(response.data.card_key);
      setNewCardKey('');
      setNewCardInitialBalanceYuan('');
      toast.success('卡密生成成功！');
      fetchUsers();
    } catch (error) {
      toast.error(error.response?.data?.detail || '生成卡密失败');
    }
  };

  const handleToggleUserStatus = async (userId, currentStatus) => {
    try {
      await axios.patch(`/api/admin/users/${userId}/toggle`, 
        {},
        { headers: { Authorization: `Bearer ${adminToken}` } }
      );
      toast.success(currentStatus ? '用户已禁用' : '用户已启用');
      fetchUsers();
    } catch (error) {
      toast.error('操作失败');
    }
  };

  const handleDeleteUser = async (userId) => {
    if (!window.confirm('确定要删除这个用户吗？此操作不可撤销。')) {
      return;
    }

    try {
      await axios.delete(`/api/admin/users/${userId}`, {
        headers: { Authorization: `Bearer ${adminToken}` }
      });
      toast.success('用户已删除');
      fetchUsers();
    } catch (error) {
      toast.error('删除用户失败');
    }
  };

  const copyToClipboard = (text) => {
    navigator.clipboard.writeText(text);
    toast.success('已复制到剪贴板');
  };

  const handleBatchGenerate = async () => {
    if (batchCount <= 0 || batchCount > 100) {
      toast.error('批量生成数量必须在 1-100 之间');
      return;
    }

    try {
      const initialBalanceCents = yuanToCents(batchInitialBalanceYuan);
      const response = await axios.post('/api/admin/batch-generate-keys',
        null,
        {
          params: { 
            count: batchCount, 
            prefix: batchPrefix,
            initial_balance_cents: Math.max(0, initialBalanceCents)
          },
          headers: { Authorization: `Bearer ${adminToken}` }
        }
      );
      
      toast.success(`成功生成 ${response.data.count} 个卡密`);
      setShowBatchModal(false);
      setBatchCount(5);
      setBatchPrefix('');
      setBatchInitialBalanceYuan('');
      fetchUsers();
      fetchStatistics();
    } catch (error) {
      toast.error(error.response?.data?.detail || '批量生成失败');
    }
  };

  const openBalanceModal = (user) => {
    setBalanceUser(user);
    setBalanceAmountYuan('');
    setBalanceReason('');
  };

  const closeBalanceModal = () => {
    setBalanceUser(null);
    setBalanceAmountYuan('');
    setBalanceReason('');
  };

  const handleAdjustBalance = async (direction) => {
    const cents = yuanToCents(balanceAmountYuan);
    if (!balanceUser || cents <= 0) {
      toast.error('请输入大于 0 的金额');
      return;
    }

    setAdjustingBalance(true);
    try {
      await axios.patch(
        `/api/admin/users/${balanceUser.id}/balance`,
        {
          delta_cents: direction === 'deduct' ? -cents : cents,
          reason: balanceReason.trim() || undefined
        },
        { headers: { Authorization: `Bearer ${adminToken}` } }
      );
      toast.success(direction === 'deduct' ? '余额已扣减' : '余额已充值');
      closeBalanceModal();
      fetchUsers();
      fetchStatistics();
    } catch (error) {
      toast.error(error.response?.data?.detail || '余额调整失败');
    } finally {
      setAdjustingBalance(false);
    }
  };

  const handleViewUserDetails = async (userId) => {
    try {
      const response = await axios.get(`/api/admin/users/${userId}/details`, {
        headers: { Authorization: `Bearer ${adminToken}` }
      });
      setUserDetails(response.data);
      setSelectedUser(userId);
      setShowUserDetails(true);
    } catch (error) {
      toast.error('获取用户详情失败');
    }
  };

  const exportUsersToCSV = () => {
    const headers = ['卡密', '状态', 'Workspace余额', 'Workspace已消费', '创建时间', '最后使用'];
    const rows = users.map(user => [
      user.card_key,
      user.is_active ? '启用' : '禁用',
      (Number(user.workspace_balance_cents || 0) / 100).toFixed(2),
      (Number(user.workspace_total_spent_cents || 0) / 100).toFixed(2),
      formatChinaDateTime(user.created_at),
      user.last_used ? formatChinaDateTime(user.last_used) : '从未使用'
    ]);
    
    const csvContent = [
      headers.join(','),
      ...rows.map(row => row.join(','))
    ].join('\n');
    
    const blob = new Blob(['\ufeff' + csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `users_${formatChinaDateForFilename()}.csv`;
    link.click();
    toast.success('用户数据已导出');
  };

  const totalWorkspaceBalanceCents = users.reduce(
    (sum, user) => sum + Number(user.workspace_balance_cents || 0),
    0
  );
  const totalWorkspaceSpentCents = users.reduce(
    (sum, user) => sum + Number(user.workspace_total_spent_cents || 0),
    0
  );

  // Login Page
  if (!isAuthenticated) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-blue-50 to-cyan-50 flex items-center justify-center p-4">
        <div className="bg-white rounded-2xl shadow-xl w-full max-w-md p-8 animate-fade-in-up">
          <div className="flex items-center justify-center mb-8">
            <div className="bg-blue-600 p-3 rounded-full">
              <Shield className="w-8 h-8 text-white" />
            </div>
          </div>
          <h1 className="text-3xl font-bold text-center mb-2 text-gray-800">
            管理后台
          </h1>
          <p className="text-center text-gray-600 mb-8">
            请使用管理员账号登录
          </p>

          <form onSubmit={handleLogin} className="space-y-6">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                用户名
              </label>
              <input
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                placeholder="请输入用户名"
                required
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                密码
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                placeholder="请输入密码"
                required
              />
            </div>

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-blue-600 hover:bg-blue-700 disabled:bg-gray-400 text-white font-semibold py-3 rounded-lg transition-colors flex items-center justify-center gap-2"
            >
              {loading ? (
                <>
                  <div className="w-5 h-5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  登录中...
                </>
              ) : (
                <>
                  <LogIn className="w-5 h-5" />
                  登录
                </>
              )}
            </button>
          </form>

          <div className="mt-6 text-center">
            <button
              onClick={() => navigate('/')}
              className="text-blue-600 hover:text-blue-700 text-sm"
            >
              返回首页
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Admin Dashboard
  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <div className="bg-white shadow-sm border-b">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <Shield className="w-8 h-8 text-blue-600" />
              <h1 className="text-2xl font-bold text-gray-800">管理后台</h1>
            </div>
            <button
              onClick={handleLogout}
              className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors"
            >
              <LogOut className="w-5 h-5" />
              退出登录
            </button>
          </div>
        </div>
      </div>

      {/* Tabs Navigation - Enhanced Design */}
      <div className="bg-gradient-to-r from-gray-50 via-white to-gray-50 border-b border-gray-200 shadow-sm">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex space-x-2 overflow-x-auto py-3">
            <button
              onClick={() => setActiveTab('dashboard')}
              className={`group relative flex items-center gap-2.5 px-6 py-3 rounded-xl font-semibold transition-all duration-300 ease-out ${
                activeTab === 'dashboard'
                  ? 'bg-gradient-to-r from-blue-600 to-blue-500 text-white shadow-lg shadow-blue-500/30 scale-105'
                  : 'bg-white text-gray-600 hover:text-blue-600 hover:bg-blue-50 hover:shadow-md border border-gray-200'
              }`}
            >
              <BarChart3 className={`w-5 h-5 transition-transform duration-300 ${
                activeTab === 'dashboard' ? 'scale-110' : 'group-hover:scale-110'
              }`} />
              <span className="whitespace-nowrap">数据面板</span>
              {activeTab === 'dashboard' && (
                <div className="absolute -bottom-1 left-1/2 transform -translate-x-1/2 w-12 h-1 bg-white rounded-full"></div>
              )}
            </button>
            
            <button
              onClick={() => setActiveTab('billing')}
              className={`group relative flex items-center gap-2.5 px-6 py-3 rounded-xl font-semibold transition-all duration-300 ease-out ${
                activeTab === 'billing'
                  ? 'bg-gradient-to-r from-green-600 to-green-500 text-white shadow-lg shadow-green-500/30 scale-105'
                  : 'bg-white text-gray-600 hover:text-green-600 hover:bg-green-50 hover:shadow-md border border-gray-200'
              }`}
            >
              <Wallet className={`w-5 h-5 transition-transform duration-300 ${
                activeTab === 'billing' ? 'scale-110' : 'group-hover:scale-110'
              }`} />
              <span className="whitespace-nowrap">计费管理</span>
              {activeTab === 'billing' && (
                <div className="absolute -bottom-1 left-1/2 transform -translate-x-1/2 w-12 h-1 bg-white rounded-full"></div>
              )}
            </button>

            
            <button
              onClick={() => setActiveTab('sessions')}
              className={`group relative flex items-center gap-2.5 px-6 py-3 rounded-xl font-semibold transition-all duration-300 ease-out ${
                activeTab === 'sessions'
                  ? 'bg-gradient-to-r from-blue-600 to-blue-500 text-white shadow-lg shadow-blue-500/30 scale-105'
                  : 'bg-white text-gray-600 hover:text-blue-600 hover:bg-blue-50 hover:shadow-md border border-gray-200'
              }`}
            >
              <Activity className={`w-5 h-5 transition-transform duration-300 ${
                activeTab === 'sessions' ? 'scale-110' : 'group-hover:scale-110'
              }`} />
              <span className="whitespace-nowrap">会话监控</span>
              {activeTab === 'sessions' && (
                <div className="absolute -bottom-1 left-1/2 transform -translate-x-1/2 w-12 h-1 bg-white rounded-full"></div>
              )}
            </button>
            
            <button
              onClick={() => setActiveTab('database')}
              className={`group relative flex items-center gap-2.5 px-6 py-3 rounded-xl font-semibold transition-all duration-300 ease-out ${
                activeTab === 'database'
                  ? 'bg-gradient-to-r from-emerald-600 to-emerald-500 text-white shadow-lg shadow-emerald-500/30 scale-105'
                  : 'bg-white text-gray-600 hover:text-emerald-600 hover:bg-emerald-50 hover:shadow-md border border-gray-200'
              }`}
            >
              <Database className={`w-5 h-5 transition-transform duration-300 ${
                activeTab === 'database' ? 'scale-110' : 'group-hover:scale-110'
              }`} />
              <span className="whitespace-nowrap">数据库管理</span>
              {activeTab === 'database' && (
                <div className="absolute -bottom-1 left-1/2 transform -translate-x-1/2 w-12 h-1 bg-white rounded-full"></div>
              )}
            </button>
            
            <button
              onClick={() => setActiveTab('config')}
              className={`group relative flex items-center gap-2.5 px-6 py-3 rounded-xl font-semibold transition-all duration-300 ease-out ${
                activeTab === 'config'
                  ? 'bg-gradient-to-r from-amber-600 to-amber-500 text-white shadow-lg shadow-amber-500/30 scale-105'
                  : 'bg-white text-gray-600 hover:text-amber-600 hover:bg-amber-50 hover:shadow-md border border-gray-200'
              }`}
            >
              <Settings className={`w-5 h-5 transition-transform duration-300 ${
                activeTab === 'config' ? 'scale-110' : 'group-hover:scale-110'
              }`} />
              <span className="whitespace-nowrap">系统配置</span>
              {activeTab === 'config' && (
                <div className="absolute -bottom-1 left-1/2 transform -translate-x-1/2 w-12 h-1 bg-white rounded-full"></div>
              )}
            </button>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Tab Content */}
        {activeTab === 'dashboard' && (
          <>
            {/* Statistics Cards */}
            {statistics && (
              <>
                {/* 第一行：用户和会话统计 */}
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-6">
                  {/* Total Users */}
                  <div className="bg-white rounded-2xl shadow-ios p-6">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm font-medium text-gray-500 mb-1">总用户数</p>
                        <p className="text-3xl font-bold text-gray-900 tracking-tight">{statistics.users.total}</p>
                        <div className="flex items-center gap-1 mt-2">
                          <span className="text-xs font-medium text-green-600 bg-green-50 px-2 py-0.5 rounded-full">
                            +{statistics.users.today_new} 今日
                          </span>
                        </div>
                      </div>
                      <div className="w-12 h-12 bg-gray-50 rounded-xl flex items-center justify-center">
                        <Users className="w-6 h-6 text-gray-600" />
                      </div>
                    </div>
                  </div>

                  {/* Active Users */}
                  <div className="bg-white rounded-2xl shadow-ios p-6">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm font-medium text-gray-500 mb-1">启用用户</p>
                        <p className="text-3xl font-bold text-gray-900 tracking-tight">{statistics.users.active}</p>
                        <div className="flex items-center gap-1 mt-2">
                          <span className="text-xs font-medium text-gray-500 bg-gray-50 px-2 py-0.5 rounded-full">
                            {statistics.users.inactive} 禁用
                          </span>
                        </div>
                      </div>
                      <div className="w-12 h-12 bg-green-50 rounded-xl flex items-center justify-center">
                        <CheckCircle className="w-6 h-6 text-green-600" />
                      </div>
                    </div>
                  </div>

                  {/* Today Active */}
                  <div className="bg-white rounded-2xl shadow-ios p-6">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm font-medium text-gray-500 mb-1">今日活跃</p>
                        <p className="text-3xl font-bold text-gray-900 tracking-tight">{statistics.users.today_active}</p>
                        <div className="flex items-center gap-1 mt-2">
                          <span className="text-xs font-medium text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                            {statistics.users.recent_active_7days} (7日)
                          </span>
                        </div>
                      </div>
                      <div className="w-12 h-12 bg-blue-50 rounded-xl flex items-center justify-center">
                        <Activity className="w-6 h-6 text-blue-600" />
                      </div>
                    </div>
                  </div>

                  {/* Total Sessions */}
                  <div className="bg-white rounded-2xl shadow-ios p-6">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm font-medium text-gray-500 mb-1">总会话数</p>
                        <p className="text-3xl font-bold text-gray-900 tracking-tight">{statistics.sessions.total}</p>
                        <div className="flex items-center gap-1 mt-2">
                          <span className="text-xs font-medium text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                            {statistics.sessions.today} 今日
                          </span>
                        </div>
                      </div>
                      <div className="w-12 h-12 bg-blue-50 rounded-xl flex items-center justify-center">
                        <Database className="w-6 h-6 text-blue-600" />
                      </div>
                    </div>
                  </div>
                </div>

                {/* 第二行：处理统计 - 统一使用白色背景，更专业 */}
                {statistics.sessions && (
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-green-50 rounded-xl flex items-center justify-center">
                          <CheckCircle className="w-5 h-5 text-green-600" />
                        </div>
                        <span className="text-xs font-medium text-gray-400">会话</span>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">会话已完成</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.sessions.completed}
                      </p>
                    </div>

                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center">
                          <Loader2 className="w-5 h-5 text-blue-600" />
                        </div>
                        <span className="text-xs font-medium text-gray-400">会话</span>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">会话运行中</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.sessions.processing}
                      </p>
                    </div>

                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-yellow-50 rounded-xl flex items-center justify-center">
                          <Clock className="w-5 h-5 text-yellow-600" />
                        </div>
                        <span className="text-xs font-medium text-gray-400">会话</span>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">会话等待中</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.sessions.queued}
                      </p>
                    </div>

                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-red-50 rounded-xl flex items-center justify-center">
                          <XCircle className="w-5 h-5 text-red-600" />
                        </div>
                        <span className="text-xs font-medium text-gray-400">会话</span>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">会话失败</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.sessions.failed}
                      </p>
                    </div>
                  </div>
                )}

                {statistics.processing && (
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
                    {/* Total Characters Processed */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-blue-50 rounded-lg flex items-center justify-center">
                          <BarChart3 className="w-5 h-5 text-blue-600" />
                        </div>
                        <span className="text-xs font-medium text-gray-400">累计</span>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">处理字符数</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.processing.total_chars_processed.toLocaleString()}
                      </p>
                    </div>

                    {/* Average Processing Time */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-orange-50 rounded-lg flex items-center justify-center">
                          <Clock className="w-5 h-5 text-orange-600" />
                        </div>
                        <span className="text-xs font-medium text-gray-400">平均</span>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">处理耗时</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {Math.round(statistics.processing.avg_processing_time)}
                        <span className="text-sm font-normal text-gray-500 ml-1">秒</span>
                      </p>
                    </div>

                    {/* Paper Polish Count */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-teal-50 rounded-lg flex items-center justify-center">
                          <FileText className="w-5 h-5 text-teal-600" />
                        </div>
                        <span className="text-xs font-medium text-gray-400">计数</span>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">论文润色</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.processing.paper_polish_count}
                      </p>
                    </div>

                    {/* Paper Polish Enhance Count */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-rose-50 rounded-lg flex items-center justify-center">
                          <TrendingUp className="w-5 h-5 text-rose-600" />
                        </div>
                        <span className="text-xs font-medium text-gray-400">计数</span>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">润色 + 增强</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.processing.paper_polish_enhance_count}
                      </p>
                    </div>
                  </div>
                )}

                {/* 第三行：Word Formatter 统计 */}
                {statistics.word_formatter && (
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-6 mb-8">
                    {/* Total Word Formatter Jobs */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center">
                          <FileText className="w-5 h-5 text-blue-600" />
                        </div>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">排版任务</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.word_formatter.total}
                      </p>
                    </div>

                    {/* Completed */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-green-50 rounded-xl flex items-center justify-center">
                          <CheckCircle className="w-5 h-5 text-green-600" />
                        </div>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">已完成</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.word_formatter.completed}
                      </p>
                    </div>

                    {/* Running */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center">
                          <Loader2 className="w-5 h-5 text-blue-600" />
                        </div>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">运行中</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.word_formatter.running}
                      </p>
                    </div>

                    {/* Pending */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-yellow-50 rounded-xl flex items-center justify-center">
                          <Clock className="w-5 h-5 text-yellow-600" />
                        </div>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">等待中</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.word_formatter.pending}
                      </p>
                    </div>

                    {/* Failed Word Formatter Jobs */}
                    <div className="bg-white rounded-2xl shadow-ios p-6">
                      <div className="flex items-center justify-between mb-4">
                        <div className="w-10 h-10 bg-red-50 rounded-xl flex items-center justify-center">
                          <XCircle className="w-5 h-5 text-red-600" />
                        </div>
                      </div>
                      <p className="text-sm font-medium text-gray-500 mb-1">排版任务失败</p>
                      <p className="text-2xl font-bold text-gray-900 tracking-tight">
                        {statistics.word_formatter.failed}
                      </p>
                    </div>
                  </div>
                )}
              </>
            )}

          </>
        )}

        {activeTab === 'billing' && (
          <>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
              <div className="bg-white rounded-2xl shadow-ios p-6 md:col-span-1">
                <div className="flex items-center gap-3 mb-5">
                  <div className="w-10 h-10 bg-green-50 rounded-xl flex items-center justify-center">
                    <Wallet className="w-5 h-5 text-green-600" />
                  </div>
                  <h2 className="text-lg font-bold text-gray-900">Workspace 定价</h2>
                </div>
                <label className="block text-sm font-medium text-gray-500 mb-2">
                  每万字价格（元）
                </label>
                <div className="flex gap-2">
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={billingPriceYuan}
                    onChange={(e) => setBillingPriceYuan(e.target.value)}
                    disabled={loadingBillingConfig || savingBillingConfig}
                    placeholder="0.00"
                    className="flex-1 px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-green-500/20 focus:border-green-500 transition-all text-sm"
                  />
                  <button
                    type="button"
                    onClick={handleSaveBillingPrice}
                    disabled={loadingBillingConfig || savingBillingConfig}
                    className="px-4 py-2.5 bg-green-600 hover:bg-green-700 disabled:bg-gray-400 text-white rounded-xl transition-colors flex items-center gap-2 text-sm font-semibold"
                  >
                    {savingBillingConfig ? (
                      <Loader2 className="w-4 h-4 animate-spin" />
                    ) : (
                      <Save className="w-4 h-4" />
                    )}
                    保存
                  </button>
                </div>
                <p className="mt-2 text-xs text-gray-400">
                  保存后 Workspace 会按提交时输入框字数预估和扣费
                </p>
              </div>

              <div className="bg-white rounded-2xl shadow-ios p-6">
                <p className="text-sm font-medium text-gray-500 mb-1">卡密总余额</p>
                <p className="text-3xl font-bold text-gray-900 tracking-tight">
                  {formatCents(totalWorkspaceBalanceCents)}
                </p>
                <p className="text-xs text-gray-400 mt-2">当前列表合计</p>
              </div>

              <div className="bg-white rounded-2xl shadow-ios p-6">
                <p className="text-sm font-medium text-gray-500 mb-1">累计消费</p>
                <p className="text-3xl font-bold text-gray-900 tracking-tight">
                  {formatCents(totalWorkspaceSpentCents)}
                </p>
                <p className="text-xs text-gray-400 mt-2">Workspace 已扣费合计</p>
              </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
              {/* Card Key Generation */}
              <div className="lg:col-span-1">
                <div className="bg-white rounded-2xl shadow-ios p-6">
                  <div className="flex items-center gap-3 mb-6">
                    <div className="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center">
                      <Key className="w-5 h-5 text-blue-600" />
                    </div>
                    <h2 className="text-lg font-bold text-gray-900">生成卡密</h2>
                  </div>

                  <form onSubmit={handleGenerateCardKey} className="space-y-4">
                    <div>
                      <label className="block text-sm font-medium text-gray-500 mb-2">
                        卡密内容（可选）
                      </label>
                      <input
                        type="text"
                        value={newCardKey}
                        onChange={(e) => setNewCardKey(e.target.value)}
                        className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
                        placeholder="留空自动生成"
                      />
                    </div>

                    <div>
                      <label className="block text-sm font-medium text-gray-500 mb-2">
                        初始余额（元）
                      </label>
                      <input
                        type="number"
                        min="0"
                        step="0.01"
                        value={newCardInitialBalanceYuan}
                        onChange={(e) => setNewCardInitialBalanceYuan(e.target.value)}
                        className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
                        placeholder="0.00"
                      />
                    </div>

                    <button
                      type="submit"
                      className="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2.5 rounded-xl transition-all active:scale-[0.98] flex items-center justify-center gap-2 text-sm shadow-sm"
                    >
                      <Plus className="w-4 h-4" />
                      生成卡密
                    </button>
                    
                    <button
                      type="button"
                      onClick={() => setShowBatchModal(true)}
                      className="w-full bg-white border border-gray-200 hover:bg-gray-50 text-gray-700 font-semibold py-2.5 rounded-xl transition-all active:scale-[0.98] flex items-center justify-center gap-2 text-sm"
                    >
                      <Key className="w-4 h-4" />
                      批量生成
                    </button>
                  </form>

                  {generatedKey && (
                    <div className="mt-6 p-4 bg-green-50/50 border border-green-100 rounded-xl">
                      <p className="text-xs font-medium text-green-700 mb-2 uppercase tracking-wide">生成的卡密</p>
                      <div className="flex items-center gap-2">
                        <code className="flex-1 bg-white px-3 py-2 rounded-lg border border-green-200 text-sm font-mono text-green-800">
                          {generatedKey}
                        </code>
                        <button
                          onClick={() => copyToClipboard(generatedKey)}
                          className="px-3 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm transition-colors shadow-sm"
                        >
                          复制
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {/* Users List */}
              <div className="lg:col-span-2">
                <div className="bg-white rounded-2xl shadow-ios overflow-hidden">
                  <div className="p-6 border-b border-gray-100">
                    <div className="flex items-center justify-between flex-wrap gap-4">
                      <div className="flex items-center gap-3">
                        <div className="w-10 h-10 bg-gray-50 rounded-xl flex items-center justify-center">
                          <Users className="w-5 h-5 text-gray-600" />
                        </div>
                        <h2 className="text-lg font-bold text-gray-900">卡密余额</h2>
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={exportUsersToCSV}
                          className="flex items-center gap-2 px-3 py-2 bg-white border border-gray-200 hover:bg-gray-50 text-gray-700 rounded-lg transition-colors text-sm font-medium"
                        >
                          <Download className="w-4 h-4" />
                          导出CSV
                        </button>
                        <button
                          onClick={() => { fetchUsers(); fetchStatistics(); fetchBillingConfig(); }}
                          disabled={loadingUsers}
                          className="flex items-center gap-2 px-3 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-400 text-white rounded-lg transition-colors text-sm font-medium shadow-sm"
                        >
                          <RefreshCw className={`w-4 h-4 ${loadingUsers ? 'animate-spin' : ''}`} />
                          刷新
                        </button>
                      </div>
                    </div>
                  </div>

                  <div className="overflow-x-auto">
                    {loadingUsers ? (
                      <div className="flex items-center justify-center py-12">
                        <div className="w-8 h-8 border-4 border-blue-600 border-t-transparent rounded-full animate-spin" />
                      </div>
                    ) : users.length === 0 ? (
                      <div className="text-center py-12 text-gray-500">
                        暂无用户数据
                      </div>
                    ) : (
                      <table className="w-full">
                        <thead className="bg-gray-50 border-b border-gray-200">
                          <tr>
                            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                              卡密
                            </th>
                            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                              Workspace 余额
                            </th>
                            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                              已消费
                            </th>
                            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                              创建时间
                            </th>
                            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                              最后使用
                            </th>
                            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                              状态
                            </th>
                            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                              操作
                            </th>
                          </tr>
                        </thead>
                        <tbody className="bg-white divide-y divide-gray-200">
                          {users.map((user) => (
                            <tr key={user.id} className="hover:bg-gray-50">
                              <td className="px-6 py-4 whitespace-nowrap">
                                <code className="text-sm font-mono text-gray-900">
                                  {user.card_key}
                                </code>
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap text-sm">
                                <div className="flex items-center gap-2">
                                  <span className={`font-semibold ${
                                    Number(user.workspace_balance_cents || 0) > 0 ? 'text-green-700' : 'text-gray-700'
                                  }`}>
                                    {formatCents(user.workspace_balance_cents)}
                                  </span>
                                  <button
                                    onClick={() => openBalanceModal(user)}
                                    className="px-2 py-1 bg-green-100 hover:bg-green-200 text-green-800 rounded transition-colors text-xs"
                                  >
                                    充值/扣减
                                  </button>
                                </div>
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-700">
                                {formatCents(user.workspace_total_spent_cents)}
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                {formatChinaDateTime(user.created_at)}
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                {user.last_used 
                                  ? formatChinaDateTime(user.last_used)
                                  : '从未使用'}
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap">
                                {user.is_active ? (
                                  <span className="inline-flex items-center gap-1 px-2 py-1 bg-green-100 text-green-800 text-xs font-medium rounded-full">
                                    <CheckCircle className="w-3 h-3" />
                                    启用
                                  </span>
                                ) : (
                                  <span className="inline-flex items-center gap-1 px-2 py-1 bg-red-100 text-red-800 text-xs font-medium rounded-full">
                                    <XCircle className="w-3 h-3" />
                                    禁用
                                  </span>
                                )}
                              </td>
                              <td className="px-6 py-4 whitespace-nowrap text-sm">
                                <div className="flex items-center gap-2 flex-wrap">
                                  <button
                                    onClick={() => handleViewUserDetails(user.id)}
                                    className="px-3 py-1 bg-blue-100 hover:bg-blue-200 text-blue-800 rounded transition-colors flex items-center gap-1"
                                  >
                                    <Eye className="w-4 h-4" />
                                    详情
                                  </button>
                                  <button
                                    onClick={() => handleToggleUserStatus(user.id, user.is_active)}
                                    className={`px-3 py-1 rounded transition-colors ${
                                      user.is_active
                                        ? 'bg-yellow-100 hover:bg-yellow-200 text-yellow-800'
                                        : 'bg-green-100 hover:bg-green-200 text-green-800'
                                    }`}
                                  >
                                    {user.is_active ? '禁用' : '启用'}
                                  </button>
                                  <button
                                    onClick={() => handleDeleteUser(user.id)}
                                    className="px-3 py-1 bg-red-100 hover:bg-red-200 text-red-800 rounded transition-colors flex items-center gap-1"
                                  >
                                    <Trash2 className="w-4 h-4" />
                                    删除
                                  </button>
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </>
        )}
        
        {/* Session Monitor Tab */}
        {activeTab === 'sessions' && (
          <SessionMonitor adminToken={adminToken} />
        )}
        
        {/* Database Manager Tab */}
        {activeTab === 'database' && (
          <DatabaseManager adminToken={adminToken} />
        )}
        
        {/* Config Manager Tab */}
        {activeTab === 'config' && (
          <ConfigManager adminToken={adminToken} />
        )}
      </div>

      {/* Batch Generation Modal */}
      {showBatchModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl shadow-2xl max-w-md w-full p-6">
            <h3 className="text-xl font-bold text-gray-800 mb-4">批量生成卡密</h3>
            
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  生成数量 (1-100)
                </label>
                <input
                  type="number"
                  min="1"
                  max="100"
                  value={batchCount}
                  onChange={(e) => setBatchCount(parseInt(e.target.value) || 1)}
                  className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              </div>
              
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  卡密前缀（可选）
                </label>
                <input
                  type="text"
                  value={batchPrefix}
                  onChange={(e) => setBatchPrefix(e.target.value)}
                  placeholder="例如: VIP-"
                  className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  初始余额（元）
                </label>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={batchInitialBalanceYuan}
                  onChange={(e) => setBatchInitialBalanceYuan(e.target.value)}
                  placeholder="0.00"
                  className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              </div>
            </div>

            <div className="flex gap-3 mt-6">
              <button
                onClick={() => setShowBatchModal(false)}
                className="flex-1 px-4 py-2 bg-gray-200 hover:bg-gray-300 text-gray-800 rounded-lg transition-colors"
              >
                取消
              </button>
              <button
                onClick={handleBatchGenerate}
                className="flex-1 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors"
              >
                生成
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Balance Adjustment Modal */}
      {balanceUser && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl shadow-2xl max-w-md w-full p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-xl font-bold text-gray-800">调整 Workspace 余额</h3>
              <button
                onClick={closeBalanceModal}
                className="text-gray-400 hover:text-gray-600"
              >
                <XCircle className="w-6 h-6" />
              </button>
            </div>

            <div className="bg-gray-50 rounded-lg p-3 mb-4 text-sm">
              <div className="text-gray-500 mb-1">卡密</div>
              <code className="font-mono text-gray-900 break-all">{balanceUser.card_key}</code>
              <div className="mt-3 grid grid-cols-2 gap-3">
                <div>
                  <div className="text-gray-500">当前余额</div>
                  <div className="font-semibold text-green-700">{formatCents(balanceUser.workspace_balance_cents)}</div>
                </div>
                <div>
                  <div className="text-gray-500">累计消费</div>
                  <div className="font-semibold text-gray-900">{formatCents(balanceUser.workspace_total_spent_cents)}</div>
                </div>
              </div>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  金额（元）
                </label>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={balanceAmountYuan}
                  onChange={(e) => setBalanceAmountYuan(e.target.value)}
                  placeholder="0.00"
                  className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">
                  原因（可选）
                </label>
                <input
                  type="text"
                  value={balanceReason}
                  onChange={(e) => setBalanceReason(e.target.value)}
                  placeholder="例如：人工充值、退款、异常扣减"
                  className="w-full px-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                />
              </div>
            </div>

            <div className="flex gap-3 mt-6">
              <button
                onClick={() => handleAdjustBalance('deduct')}
                disabled={adjustingBalance}
                className="flex-1 px-4 py-2 bg-red-100 hover:bg-red-200 disabled:bg-gray-100 text-red-800 rounded-lg transition-colors"
              >
                扣减
              </button>
              <button
                onClick={() => handleAdjustBalance('recharge')}
                disabled={adjustingBalance}
                className="flex-1 px-4 py-2 bg-green-600 hover:bg-green-700 disabled:bg-gray-400 text-white rounded-lg transition-colors"
              >
                充值
              </button>
            </div>
          </div>
        </div>
      )}

      {/* User Details Modal */}
      {showUserDetails && userDetails && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-xl shadow-2xl max-w-2xl w-full p-6 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
              <h3 className="text-xl font-bold text-gray-800">用户详细信息</h3>
              <button
                onClick={() => setShowUserDetails(false)}
                className="text-gray-400 hover:text-gray-600"
              >
                <XCircle className="w-6 h-6" />
              </button>
            </div>

            {/* User Info */}
            <div className="bg-gray-50 rounded-lg p-4 mb-6">
              <h4 className="font-semibold text-gray-800 mb-3">基本信息</h4>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <span className="text-gray-600">卡密：</span>
                  <code className="ml-2 font-mono text-blue-600">{userDetails.user.card_key}</code>
                </div>
                <div>
                  <span className="text-gray-600">状态：</span>
                  <span className={`ml-2 ${userDetails.user.is_active ? 'text-green-600' : 'text-red-600'}`}>
                    {userDetails.user.is_active ? '启用' : '禁用'}
                  </span>
                </div>
                <div>
                  <span className="text-gray-600">创建时间：</span>
                  <span className="ml-2">{formatChinaDateTime(userDetails.user.created_at)}</span>
                </div>
                <div>
                  <span className="text-gray-600">最后使用：</span>
                  <span className="ml-2">
                    {userDetails.user.last_used 
                      ? formatChinaDateTime(userDetails.user.last_used)
                      : '从未使用'}
                  </span>
                </div>
              </div>
            </div>

            {/* Statistics */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
              <div className="bg-blue-50 rounded-lg p-4 text-center">
                <p className="text-2xl font-bold text-blue-600">{userDetails.statistics.total_sessions}</p>
                <p className="text-xs text-gray-600 mt-1">总会话数</p>
              </div>
              <div className="bg-green-50 rounded-lg p-4 text-center">
                <p className="text-2xl font-bold text-green-600">{userDetails.statistics.completed_sessions}</p>
                <p className="text-xs text-gray-600 mt-1">完成会话</p>
              </div>
              <div className="bg-blue-50 rounded-lg p-4 text-center">
                <p className="text-2xl font-bold text-blue-600">{userDetails.statistics.total_segments}</p>
                <p className="text-xs text-gray-600 mt-1">处理段落</p>
              </div>
              <div className="bg-orange-50 rounded-lg p-4 text-center">
                <p className="text-2xl font-bold text-orange-600">{userDetails.statistics.completed_segments}</p>
                <p className="text-xs text-gray-600 mt-1">完成段落</p>
              </div>
            </div>

            {/* Recent Sessions */}
            {userDetails.recent_sessions.length > 0 && (
              <div>
                <h4 className="font-semibold text-gray-800 mb-3">最近会话</h4>
                <div className="space-y-2">
                  {userDetails.recent_sessions.map((session) => (
                    <div key={session.id} className="bg-gray-50 rounded-lg p-3 flex items-center justify-between">
                      <div className="flex items-center gap-3">
                        <Activity className="w-4 h-4 text-gray-400" />
                        <div>
                          <p className="text-sm font-medium text-gray-800">会话 #{session.id}</p>
                          <p className="text-xs text-gray-500">
                            {formatChinaDateTime(session.created_at)}
                          </p>
                        </div>
                      </div>
                      <span className={`px-2 py-1 rounded text-xs font-medium ${
                        session.status === 'completed' 
                          ? 'bg-green-100 text-green-800'
                          : 'bg-yellow-100 text-yellow-800'
                      }`}>
                        {session.status === 'completed' ? '已完成' : '处理中'}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <button
              onClick={() => setShowUserDetails(false)}
              className="w-full mt-6 px-4 py-2 bg-gray-200 hover:bg-gray-300 text-gray-800 rounded-lg transition-colors"
            >
              关闭
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default AdminDashboard;
