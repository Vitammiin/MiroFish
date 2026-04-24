import axios from 'axios'
import i18n from '../i18n'

const sanitizeApiBaseUrl = (rawBaseUrl) => {
  const fallbackBaseUrl = '/api'

  if (!rawBaseUrl) return fallbackBaseUrl

  const normalizedBaseUrl = rawBaseUrl.trim().replace(/\/+$/, '')
  if (!normalizedBaseUrl) return fallbackBaseUrl

  if (normalizedBaseUrl.endsWith('/api/ai')) {
    const rewrittenBaseUrl = normalizedBaseUrl.slice(0, -3)
    console.warn(
      `[api] Legacy base URL "${rawBaseUrl}" detected. Rewriting requests to "${rewrittenBaseUrl}".`
    )
    return rewrittenBaseUrl
  }

  return normalizedBaseUrl
}

const normalizeRequestError = (error, fallbackMessage = 'Request failed') => {
  const serverMessage = error?.response?.data?.error || error?.response?.data?.message
  const isBackendUnavailable =
    error?.code === 'ERR_NETWORK' &&
    (error?.message === 'Network Error' || !error?.response)
  const helpfulNetworkMessage =
    'Backend API недоступен. Запустите backend и проверьте LLM_API_KEY / ZEP_API_KEY в MiroFish/.env.'
  const normalizedError = new Error(
    serverMessage || (isBackendUnavailable ? helpfulNetworkMessage : error?.message) || fallbackMessage
  )

  normalizedError.response = error?.response
  normalizedError.code = error?.code

  const status = error?.response?.status
  const isNetworkError = !status || error?.code === 'ECONNABORTED' || error?.message === 'Network Error'
  const isRetriableStatus = status === 429 || status === 502 || status === 503 || status === 504
  normalizedError.isRetriable = isNetworkError || isRetriableStatus

  return normalizedError
}

const apiBaseUrl = sanitizeApiBaseUrl(import.meta.env.VITE_API_BASE_URL)

// 创建axios实例
const service = axios.create({
  // В dev используем относительный /api и Vite proxy, чтобы не хардкодить localhost:5001 в браузере.
  baseURL: apiBaseUrl,
  timeout: 300000, // 5分钟超时（本体生成可能需要较长时间）
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
service.interceptors.request.use(
  config => {
    config.headers['Accept-Language'] = i18n.global.locale.value
    return config
  },
  error => {
    console.error('Request error:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器（容错重试机制）
service.interceptors.response.use(
  response => {
    const res = response.data
    
    // 如果返回的状态码不是success，则抛出错误
    if (!res.success && res.success !== undefined) {
      console.error('API Error:', res.error || res.message || 'Unknown error')
      const apiError = new Error(res.error || res.message || 'Error')
      apiError.isRetriable = false
      return Promise.reject(apiError)
    }
    
    return res
  },
  error => {
    const traceback = error.response?.data?.traceback

    console.error('Response error:', error)
    if (error.response?.data?.error || error.response?.data?.message) {
      console.error('Server error message:', error.response?.data?.error || error.response?.data?.message)
    }
    if (traceback) {
      console.error('Server traceback:', traceback)
    }
    
    // 处理超时
    if (error.code === 'ECONNABORTED' && error.message.includes('timeout')) {
      console.error('Request timeout')
    }
    
    // 处理网络错误
    if (error.message === 'Network Error') {
      console.error('Backend API unavailable - check backend startup and MiroFish/.env')
    }
    
    return Promise.reject(normalizeRequestError(error))
  }
)

// 带重试的请求函数
export const requestWithRetry = async (requestFn, maxRetries = 3, delay = 1000) => {
  for (let i = 0; i < maxRetries; i++) {
    try {
      return await requestFn()
    } catch (error) {
      const shouldRetry = error.isRetriable === true

      if (!shouldRetry || i === maxRetries - 1) throw error
      
      console.warn(`Request failed, retrying (${i + 1}/${maxRetries})...`)
      await new Promise(resolve => setTimeout(resolve, delay * Math.pow(2, i)))
    }
  }
}

export default service
