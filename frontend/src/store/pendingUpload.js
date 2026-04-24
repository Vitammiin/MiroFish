/**
 * 临时存储待上传的文件和需求。
 * 除了 Vue 内存状态外，还会把文件持久化到 IndexedDB，
 * 这样在跳转到 /process/new 时即使发生整页刷新也不会丢失。
 */
import { reactive } from 'vue'

const DB_NAME = 'mirofish-pending-upload'
const STORE_NAME = 'uploads'
const RECORD_ID = 'latest'

const state = reactive({
  files: [],
  simulationRequirement: '',
  isPending: false
})

let restorePromise = null

function applyState(payload = null) {
  if (payload?.isPending && Array.isArray(payload.files) && payload.files.length > 0) {
    state.files = payload.files
    state.simulationRequirement = payload.simulationRequirement || ''
    state.isPending = true
    return
  }

  state.files = []
  state.simulationRequirement = ''
  state.isPending = false
}

function openDb() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error('IndexedDB is not available in this browser'))
      return
    }

    const request = window.indexedDB.open(DB_NAME, 1)

    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id' })
      }
    }

    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error || new Error('Failed to open IndexedDB'))
  })
}

async function writeRecord(payload) {
  const db = await openDb()

  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)

    tx.oncomplete = () => {
      db.close()
      resolve()
    }
    tx.onerror = () => {
      db.close()
      reject(tx.error || new Error('Failed to write pending upload'))
    }

    store.put({
      id: RECORD_ID,
      files: payload.files,
      simulationRequirement: payload.simulationRequirement,
      isPending: payload.isPending,
      updatedAt: Date.now()
    })
  })
}

async function readRecord() {
  const db = await openDb()

  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly')
    const store = tx.objectStore(STORE_NAME)
    const request = store.get(RECORD_ID)

    request.onsuccess = () => {
      db.close()
      resolve(request.result || null)
    }
    request.onerror = () => {
      db.close()
      reject(request.error || new Error('Failed to read pending upload'))
    }
  })
}

async function deleteRecord() {
  const db = await openDb()

  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite')
    const store = tx.objectStore(STORE_NAME)

    tx.oncomplete = () => {
      db.close()
      resolve()
    }
    tx.onerror = () => {
      db.close()
      reject(tx.error || new Error('Failed to clear pending upload'))
    }

    store.delete(RECORD_ID)
  })
}

export async function setPendingUpload(files, requirement) {
  const payload = {
    files: Array.from(files || []),
    simulationRequirement: requirement || '',
    isPending: Array.isArray(files) ? files.length > 0 : Array.from(files || []).length > 0
  }

  applyState(payload)
  try {
    await writeRecord(payload)
  } catch (error) {
    console.error('Persist pending upload failed:', error)
  }
}

export function getPendingUpload() {
  return {
    files: state.files,
    simulationRequirement: state.simulationRequirement,
    isPending: state.isPending
  }
}

export async function restorePendingUpload(force = false) {
  if (state.isPending && !force) {
    return getPendingUpload()
  }

  if (!restorePromise || force) {
    restorePromise = readRecord()
      .then((record) => {
        applyState(record)
        return getPendingUpload()
      })
      .catch((error) => {
        console.error('Restore pending upload failed:', error)
        applyState(null)
        return getPendingUpload()
      })
      .finally(() => {
        restorePromise = null
      })
  }

  return restorePromise
}

export async function clearPendingUpload() {
  applyState(null)
  try {
    await deleteRecord()
  } catch (error) {
    console.error('Clear pending upload failed:', error)
  }
}

export default state
