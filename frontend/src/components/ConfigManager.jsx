import React, { useState, useEffect } from 'react';
import axios from 'axios';
import { toast } from 'react-hot-toast';
import { Settings, Save, RefreshCw, Brain, Plus, Pencil, Trash2, Star, X, Check } from 'lucide-react';
import { adminModelProfilesAPI } from '../api';

const ConfigManager = ({ adminToken }) => {
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  // Model Profiles state
  const [profiles, setProfiles] = useState([]);
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editingProfile, setEditingProfile] = useState(null);
  const [profileForm, setProfileForm] = useState({
    name: '',
    model: '',
    api_key: '',
    base_url: '',
    is_active: true,
    is_default: false,
    sort_order: 0,
  });

  const [formData, setFormData] = useState({
    MAX_CONCURRENT_USERS: '',
    HISTORY_COMPRESSION_THRESHOLD: '',
    COMPRESSION_MODEL: '',
    COMPRESSION_API_KEY: '',
    COMPRESSION_BASE_URL: '',
    DEFAULT_USAGE_LIMIT: '',
    SEGMENT_SKIP_THRESHOLD: '',
    MAX_UPLOAD_FILE_SIZE_MB: '',
    API_REQUEST_INTERVAL: '',
    THINKING_MODE_ENABLED: true,
    THINKING_MODE_EFFORT: 'high'
  });

  useEffect(() => {
    fetchConfig();
    fetchProfiles();
  }, []);

  const fetchConfig = async () => {
    setLoading(true);
    try {
      const response = await axios.get('/api/admin/config', {
        headers: { Authorization: `Bearer ${adminToken}` }
      });

      setFormData({
        MAX_CONCURRENT_USERS: response.data.system.max_concurrent_users?.toString() || '',
        HISTORY_COMPRESSION_THRESHOLD: response.data.system.history_compression_threshold?.toString() || '',
        COMPRESSION_MODEL: response.data.compression?.model || '',
        COMPRESSION_API_KEY: response.data.compression?.api_key || '',
        COMPRESSION_BASE_URL: response.data.compression?.base_url || '',
        DEFAULT_USAGE_LIMIT: response.data.system.default_usage_limit?.toString() || '',
        SEGMENT_SKIP_THRESHOLD: response.data.system.segment_skip_threshold?.toString() || '',
        MAX_UPLOAD_FILE_SIZE_MB: response.data.system.max_upload_file_size_mb?.toString() || '',
        API_REQUEST_INTERVAL: response.data.system.api_request_interval?.toString() || '6',
        THINKING_MODE_ENABLED: response.data.thinking?.enabled ?? true,
        THINKING_MODE_EFFORT: response.data.thinking?.effort || 'high'
      });
    } catch (error) {
      toast.error('获取配置失败');
    } finally {
      setLoading(false);
    }
  };

  const fetchProfiles = async () => {
    setProfilesLoading(true);
    try {
      const response = await adminModelProfilesAPI.list(adminToken);
      setProfiles(response.data);
    } catch (error) {
      toast.error('获取模型列表失败');
    } finally {
      setProfilesLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const updates = {};
      Object.keys(formData).forEach(key => {
        const value = formData[key];
        if (typeof value === 'boolean') {
          updates[key] = value.toString();
        } else if (typeof value === 'string' && value.trim()) {
          updates[key] = value.trim();
        }
      });

      const response = await axios.post('/api/admin/config', updates, {
        headers: { Authorization: `Bearer ${adminToken}` }
      });

      toast.success(response.data.message);
      fetchConfig();
    } catch (error) {
      toast.error(error.response?.data?.detail || '保存配置失败');
    } finally {
      setSaving(false);
    }
  };

  // Model Profile CRUD
  const openAddModal = () => {
    setEditingProfile(null);
    setProfileForm({
      name: '',
      model: '',
      api_key: '',
      base_url: '',
      is_active: true,
      is_default: false,
      sort_order: 0,
    });
    setShowModal(true);
  };

  const openEditModal = (profile) => {
    setEditingProfile(profile);
    setProfileForm({
      name: profile.name,
      model: profile.model,
      api_key: profile.api_key,
      base_url: profile.base_url,
      is_active: profile.is_active,
      is_default: profile.is_default,
      sort_order: profile.sort_order,
    });
    setShowModal(true);
  };

  const handleProfileSubmit = async () => {
    try {
      if (editingProfile) {
        await adminModelProfilesAPI.update(editingProfile.id, profileForm, adminToken);
        toast.success('模型已更新');
      } else {
        await adminModelProfilesAPI.create(profileForm, adminToken);
        toast.success('模型已添加');
      }
      setShowModal(false);
      fetchProfiles();
    } catch (error) {
      toast.error(error.response?.data?.detail || '操作失败');
    }
  };

  const handleDeleteProfile = async (id) => {
    if (!confirm('确定删除此模型配置？')) return;
    try {
      await adminModelProfilesAPI.delete(id, adminToken);
      toast.success('已删除');
      fetchProfiles();
    } catch (error) {
      toast.error(error.response?.data?.detail || '删除失败');
    }
  };

  const handleSetDefault = async (id) => {
    try {
      await adminModelProfilesAPI.setDefault(id, adminToken);
      toast.success('已设为默认模型');
      fetchProfiles();
    } catch (error) {
      toast.error(error.response?.data?.detail || '设置失败');
    }
  };

  const maskApiKey = (key) => {
    if (!key || key.length < 8) return '***';
    return key.substring(0, 4) + '***' + key.substring(key.length - 4);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="w-8 h-8 border-4 border-blue-600 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* API 配置教程 */}
      {/* 模型管理 */}
      <div className="bg-white rounded-2xl shadow-ios p-6">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-teal-50 rounded-xl flex items-center justify-center">
              <Settings className="w-5 h-5 text-teal-600" />
            </div>
            <div>
              <h3 className="text-lg font-bold text-gray-900">模型管理</h3>
              <p className="text-xs text-gray-400">管理可供用户选择的 AI 模型配置</p>
            </div>
          </div>
          <button
            onClick={openAddModal}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium rounded-xl transition-all active:scale-[0.98]"
          >
            <Plus className="w-4 h-4" />
            添加模型
          </button>
        </div>

        {profilesLoading ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-6 h-6 border-3 border-blue-600 border-t-transparent rounded-full animate-spin" />
          </div>
        ) : profiles.length === 0 ? (
          <div className="text-center py-8 text-gray-400 text-sm">
            暂无模型配置，点击上方按钮添加
          </div>
        ) : (
          <div className="space-y-3">
            {profiles.map((profile) => (
              <div
                key={profile.id}
                className={`flex items-center justify-between p-4 rounded-xl border transition-all ${
                  profile.is_default
                    ? 'border-blue-300 bg-blue-50/50'
                    : profile.is_active
                    ? 'border-gray-200 bg-gray-50'
                    : 'border-gray-100 bg-gray-50/50 opacity-60'
                }`}
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-900 text-sm">{profile.name}</span>
                    {profile.is_default && (
                      <span className="px-2 py-0.5 bg-blue-100 text-blue-700 text-xs rounded-full font-medium">默认</span>
                    )}
                    {!profile.is_active && (
                      <span className="px-2 py-0.5 bg-gray-100 text-gray-500 text-xs rounded-full">已禁用</span>
                    )}
                  </div>
                  <div className="mt-1 flex items-center gap-4 text-xs text-gray-400">
                    <span>模型: <span className="text-gray-600 font-mono">{profile.model}</span></span>
                    <span>API Key: <span className="text-gray-500 font-mono">{maskApiKey(profile.api_key)}</span></span>
                  </div>
                  <div className="mt-0.5 text-xs text-gray-400 truncate">
                    Base URL: <span className="text-gray-500 font-mono">{profile.base_url}</span>
                  </div>
                </div>
                <div className="flex items-center gap-2 ml-4">
                  {!profile.is_default && (
                    <button
                      onClick={() => handleSetDefault(profile.id)}
                      className="p-2 text-gray-400 hover:text-yellow-500 hover:bg-yellow-50 rounded-lg transition-all"
                      title="设为默认"
                    >
                      <Star className="w-4 h-4" />
                    </button>
                  )}
                  <button
                    onClick={() => openEditModal(profile)}
                    className="p-2 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded-lg transition-all"
                    title="编辑"
                  >
                    <Pencil className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => handleDeleteProfile(profile.id)}
                    className="p-2 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-all"
                    title="删除"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 模型编辑弹窗 */}
      {showModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg mx-4 p-6 max-h-[90vh] overflow-y-auto">
            <div className="flex items-center justify-between mb-6">
              <h3 className="text-lg font-bold text-gray-900">
                {editingProfile ? '编辑模型' : '添加模型'}
              </h3>
              <button
                onClick={() => setShowModal(false)}
                className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-all"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">显示名称</label>
                <input
                  type="text"
                  value={profileForm.name}
                  onChange={(e) => setProfileForm({ ...profileForm, name: e.target.value })}
                  placeholder="Gemini 2.5 Pro"
                  className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">模型 ID</label>
                <input
                  type="text"
                  value={profileForm.model}
                  onChange={(e) => setProfileForm({ ...profileForm, model: e.target.value })}
                  placeholder="gemini-2.5-pro"
                  className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm font-mono"
                />
                <p className="mt-1.5 text-xs text-gray-400">
                  推荐: gemini-2.5-pro, gpt-4o, claude-sonnet-4-20250514
                </p>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">API Key</label>
                <input
                  type="password"
                  value={profileForm.api_key}
                  onChange={(e) => setProfileForm({ ...profileForm, api_key: e.target.value })}
                  placeholder="sk-... 或 AIzaSy..."
                  className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm font-mono"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">Base URL</label>
                <input
                  type="text"
                  value={profileForm.base_url}
                  onChange={(e) => setProfileForm({ ...profileForm, base_url: e.target.value })}
                  placeholder="https://api.openai.com/v1"
                  className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
                />
                <p className="mt-1.5 text-xs text-gray-400">
                  必须以 /v1 结尾。Gemini: https://generativelanguage.googleapis.com/v1beta/openai
                </p>
              </div>

              <div className="flex items-center gap-6">
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={profileForm.is_active}
                    onChange={(e) => setProfileForm({ ...profileForm, is_active: e.target.checked })}
                    className="w-4 h-4 text-blue-600 border-gray-300 rounded focus:ring-blue-500"
                  />
                  <span className="text-sm text-gray-700">启用</span>
                </label>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={profileForm.is_default}
                    onChange={(e) => setProfileForm({ ...profileForm, is_default: e.target.checked })}
                    className="w-4 h-4 text-blue-600 border-gray-300 rounded focus:ring-blue-500"
                  />
                  <span className="text-sm text-gray-700">默认模型</span>
                </label>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-500 mb-2">排序权重</label>
                <input
                  type="number"
                  value={profileForm.sort_order}
                  onChange={(e) => setProfileForm({ ...profileForm, sort_order: parseInt(e.target.value) || 0 })}
                  className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
                />
                <p className="mt-1.5 text-xs text-gray-400">数字越小排序越靠前</p>
              </div>
            </div>

            <div className="flex gap-3 mt-6">
              <button
                onClick={() => setShowModal(false)}
                className="flex-1 px-4 py-2.5 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-xl transition-all text-sm font-medium"
              >
                取消
              </button>
              <button
                onClick={handleProfileSubmit}
                disabled={!profileForm.name || !profileForm.model || !profileForm.api_key || !profileForm.base_url}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300 text-white rounded-xl transition-all text-sm font-medium"
              >
                <Check className="w-4 h-4" />
                {editingProfile ? '保存修改' : '添加'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 思考模式配置 */}
      <div className="bg-white rounded-2xl shadow-ios p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center">
            <Brain className="w-5 h-5 text-blue-600" />
          </div>
          <h3 className="text-lg font-bold text-gray-900">思考模式配置</h3>
        </div>

        <div className="space-y-5">
          {/* 启用开关 */}
          <div className="flex items-center justify-between">
            <div>
              <label className="block text-sm font-medium text-gray-700">
                启用思考模式
              </label>
              <p className="text-xs text-gray-400 mt-1">
                开启后模型会进行深度推理，可能增加响应时间和 token 消耗
              </p>
            </div>
            <button
              type="button"
              onClick={() => setFormData({
                ...formData,
                THINKING_MODE_ENABLED: !formData.THINKING_MODE_ENABLED
              })}
              className={`relative w-12 h-7 rounded-full transition-colors duration-200 ${
                formData.THINKING_MODE_ENABLED
                  ? 'bg-blue-600'
                  : 'bg-gray-200'
              }`}
            >
              <span className={`absolute top-0.5 left-0.5 w-6 h-6 bg-white rounded-full shadow transition-transform ${
                formData.THINKING_MODE_ENABLED
                  ? 'translate-x-5'
                  : 'translate-x-0'
              }`} />
            </button>
          </div>

          {/* 思考强度选择器 */}
          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              思考强度
            </label>
            <select
              value={formData.THINKING_MODE_EFFORT}
              onChange={(e) => setFormData({...formData, THINKING_MODE_EFFORT: e.target.value})}
              disabled={!formData.THINKING_MODE_ENABLED}
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <option value="none">无推理 (最低延迟)</option>
              <option value="low">轻度推理</option>
              <option value="medium">中度推理</option>
              <option value="high">深度推理 (推荐)</option>
              <option value="xhigh">极深推理 (仅部分模型支持)</option>
            </select>
            <p className="mt-1.5 text-xs text-gray-400">
              更高的强度会增加推理 token 消耗和响应时间，但可能获得更好的结果
            </p>
          </div>
        </div>
      </div>

      {/* 系统配置 */}
      <div className="bg-white rounded-2xl shadow-ios p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="w-10 h-10 bg-orange-50 rounded-xl flex items-center justify-center">
            <Settings className="w-5 h-5 text-orange-600" />
          </div>
          <div>
            <h3 className="text-lg font-bold text-gray-900">系统配置</h3>
            <p className="text-xs text-gray-400">压缩模型与运行参数设置</p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              最大并发用户数
            </label>
            <input
              type="number"
              value={formData.MAX_CONCURRENT_USERS}
              onChange={(e) => setFormData({...formData, MAX_CONCURRENT_USERS: e.target.value})}
              placeholder="5"
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
            />
            <p className="mt-1.5 text-xs text-gray-400">同时处理任务的最大数量</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              历史压缩阈值（字符）
            </label>
            <input
              type="number"
              value={formData.HISTORY_COMPRESSION_THRESHOLD}
              onChange={(e) => setFormData({...formData, HISTORY_COMPRESSION_THRESHOLD: e.target.value})}
              placeholder="5000"
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
            />
            <p className="mt-1.5 text-xs text-gray-400">超过此字数时自动压缩历史记录</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              压缩模型
            </label>
            <input
              type="text"
              value={formData.COMPRESSION_MODEL}
              onChange={(e) => setFormData({...formData, COMPRESSION_MODEL: e.target.value})}
              placeholder="gemini-2.5-pro"
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
            />
            <p className="mt-1.5 text-xs text-gray-400">用于压缩历史记录的模型</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              压缩 API Key
            </label>
            <input
              type="password"
              value={formData.COMPRESSION_API_KEY}
              onChange={(e) => setFormData({...formData, COMPRESSION_API_KEY: e.target.value})}
              placeholder="sk-... 或 AIzaSy..."
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm font-mono"
            />
            <p className="mt-1.5 text-xs text-gray-400">可与其他模型使用相同的 Key</p>
          </div>

          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-500 mb-2">
              压缩 Base URL
            </label>
            <input
              type="text"
              value={formData.COMPRESSION_BASE_URL}
              onChange={(e) => setFormData({...formData, COMPRESSION_BASE_URL: e.target.value})}
              placeholder="https://api.openai.com/v1"
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
            />
            <p className="mt-1.5 text-xs text-gray-400">可与其他模型使用相同的地址</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              排版功能默认使用次数限制
            </label>
            <input
              type="number"
              value={formData.DEFAULT_USAGE_LIMIT}
              onChange={(e) => setFormData({...formData, DEFAULT_USAGE_LIMIT: e.target.value})}
              placeholder="1"
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
            />
            <p className="mt-1.5 text-xs text-gray-400">仅用于旧排版相关功能，Workspace 卡密按余额计费</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              段落跳过阈值（字符）
            </label>
            <input
              type="number"
              value={formData.SEGMENT_SKIP_THRESHOLD}
              onChange={(e) => setFormData({...formData, SEGMENT_SKIP_THRESHOLD: e.target.value})}
              placeholder="15"
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
            />
            <p className="mt-1.5 text-xs text-gray-400">小于此字数的段落将被识别为标题并跳过</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              API 请求间隔（秒）
            </label>
            <input
              type="number"
              value={formData.API_REQUEST_INTERVAL}
              onChange={(e) => setFormData({...formData, API_REQUEST_INTERVAL: e.target.value})}
              placeholder="6"
              min="0"
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
            />
            <p className="mt-1.5 text-xs text-gray-400">每个段落处理完成后的等待时间，用于避免触发 API 频率限制 (RATE_LIMIT)，0 表示无间隔</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-500 mb-2">
              Word 排版文件大小限制 (MB)
            </label>
            <input
              type="number"
              value={formData.MAX_UPLOAD_FILE_SIZE_MB}
              onChange={(e) => setFormData({...formData, MAX_UPLOAD_FILE_SIZE_MB: e.target.value})}
              placeholder="0"
              min="0"
              className="w-full px-4 py-2.5 bg-gray-50 border border-gray-200 rounded-xl focus:bg-white focus:ring-2 focus:ring-blue-500/20 focus:border-blue-500 transition-all text-sm"
            />
            <p className="mt-1.5 text-xs text-gray-400">0 表示无限制</p>
          </div>
        </div>
      </div>

      {/* 操作按钮 */}
      <div className="flex gap-4">
        <button
          onClick={() => { fetchConfig(); fetchProfiles(); }}
          disabled={loading}
          className="flex items-center gap-2 px-6 py-3 bg-white border border-gray-200 hover:bg-gray-50 disabled:bg-gray-50 text-gray-700 rounded-xl transition-all active:scale-[0.98] font-medium shadow-sm"
        >
          <RefreshCw className={`w-5 h-5 ${loading ? 'animate-spin' : ''}`} />
          刷新
        </button>
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex-1 flex items-center justify-center gap-2 px-6 py-3 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300 text-white rounded-xl transition-all active:scale-[0.98] font-semibold shadow-sm"
        >
          {saving ? (
            <>
              <div className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              保存中...
            </>
          ) : (
            <>
              <Save className="w-5 h-5" />
              保存配置
            </>
          )}
        </button>
      </div>

      <div className="bg-green-50/50 border border-green-100 rounded-xl p-4">
        <p className="text-sm font-medium text-green-800 flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-green-500"></span>
          配置修改后会立即生效，无需重启服务！
        </p>
      </div>
    </div>
  );
};

export default ConfigManager;
