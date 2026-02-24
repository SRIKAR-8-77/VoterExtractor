import { useState, useRef, useCallback } from 'react'
import './App.css'

const API_BASE = import.meta.env.VITE_API_URL || ''

function App() {
  const [files, setFiles] = useState([])
  const [dragActive, setDragActive] = useState(false)
  const [jobId, setJobId] = useState(null)
  const [progress, setProgress] = useState(0)
  const [stage, setStage] = useState('')
  const [status, setStatus] = useState('idle') // idle | uploading | processing | completed | failed
  const [error, setError] = useState('')
  const [detail, setDetail] = useState('')
  const [maxPages, setMaxPages] = useState('')
  const fileInputRef = useRef(null)
  const jobDoneRef = useRef(false)

  const handleDrag = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true)
    } else if (e.type === 'dragleave') {
      setDragActive(false)
    }
  }, [])

  const handleDrop = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    const droppedFiles = Array.from(e.dataTransfer.files).filter(f =>
      f.name.toLowerCase().endsWith('.pdf')
    )
    if (droppedFiles.length > 0) {
      setFiles(prev => [...prev, ...droppedFiles])
    }
  }, [])

  const handleFileSelect = (e) => {
    const selected = Array.from(e.target.files).filter(f =>
      f.name.toLowerCase().endsWith('.pdf')
    )
    setFiles(prev => [...prev, ...selected])
  }

  const removeFile = (index) => {
    setFiles(prev => prev.filter((_, i) => i !== index))
  }

  const startProcessing = async () => {
    if (files.length === 0) return

    setStatus('uploading')
    setProgress(0)
    setStage('Uploading files...')
    setError('')
    jobDoneRef.current = false

    try {
      const formData = new FormData()
      files.forEach(f => formData.append('files', f))

      const params = maxPages ? `?max_pages=${maxPages}` : ''
      const res = await fetch(`${API_BASE}/api/process${params}`, {
        method: 'POST',
        body: formData,
      })

      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Upload failed')
      }

      const data = await res.json()
      setJobId(data.job_id)
      setStatus('processing')

      // Start SSE progress tracking
      const evtSource = new EventSource(`${API_BASE}/api/progress/${data.job_id}`)

      evtSource.onmessage = (event) => {
        try {
          const d = JSON.parse(event.data)
          setProgress(d.progress)
          setStage(d.stage)
          setDetail(d.detail || '')

          if (d.status === 'completed') {
            jobDoneRef.current = true
            setStatus('completed')
            evtSource.close()
          } else if (d.status === 'failed') {
            jobDoneRef.current = true
            setStatus('failed')
            setError(d.error || d.detail || 'Processing failed')
            evtSource.close()
          }
        } catch (e) {
          console.error('SSE parse error:', e, event.data)
        }
      }

      evtSource.onerror = () => {
        evtSource.close()
        if (!jobDoneRef.current) {
          // SSE dropped — poll once to get final status
          fetch(`${API_BASE}/api/jobs/${data.job_id}`)
            .then(r => r.json())
            .then(d => {
              if (d.status === 'completed') {
                jobDoneRef.current = true
                setStatus('completed')
                setProgress(100)
                setDetail(d.detail || '')
              } else if (d.status === 'failed') {
                jobDoneRef.current = true
                setStatus('failed')
                setError(d.error || 'Processing failed')
              }
              // still processing — keep polling
              else if (d.status === 'processing') {
                const poll = setInterval(() => {
                  fetch(`${API_BASE}/api/jobs/${data.job_id}`)
                    .then(r => r.json())
                    .then(d2 => {
                      setProgress(d2.progress)
                      setStage(d2.stage)
                      setDetail(d2.detail || '')
                      if (d2.status === 'completed') {
                        jobDoneRef.current = true
                        setStatus('completed')
                        clearInterval(poll)
                      } else if (d2.status === 'failed') {
                        jobDoneRef.current = true
                        setStatus('failed')
                        setError(d2.error || 'Processing failed')
                        clearInterval(poll)
                      }
                    })
                    .catch(() => clearInterval(poll))
                }, 3000)
              }
            })
            .catch(() => { })
        }
      }
    } catch (err) {
      setStatus('failed')
      setError(err.message)
    }
  }

  const downloadResult = () => {
    if (jobId) {
      window.open(`${API_BASE}/api/download/${jobId}`, '_blank')
    }
  }

  const resetAll = () => {
    setFiles([])
    setJobId(null)
    setProgress(0)
    setStage('')
    setStatus('idle')
    setError('')
    setDetail('')
    setMaxPages('')
    jobDoneRef.current = false
  }

  const formatFileSize = (bytes) => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  return (
    <div className="app">
      <div className="container">
        {/* Header */}
        <header className="header">
          <div className="logo">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
              <polyline points="10 9 9 9 8 9" />
            </svg>
            <h1>PDF → Excel</h1>
          </div>
          <p className="subtitle">Upload voter roll PDFs and get structured Excel files</p>
        </header>

        {/* Upload Zone */}
        {status === 'idle' && (
          <div className="upload-section">
            <div
              className={`drop-zone ${dragActive ? 'active' : ''}`}
              onDragEnter={handleDrag}
              onDragLeave={handleDrag}
              onDragOver={handleDrag}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
            >
              <div className="drop-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="17 8 12 3 7 8" />
                  <line x1="12" y1="3" x2="12" y2="15" />
                </svg>
              </div>
              <p className="drop-text">
                {dragActive ? 'Drop PDFs here' : 'Drag & drop PDFs here'}
              </p>
              <p className="drop-hint">or click to browse</p>
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf"
                multiple
                onChange={handleFileSelect}
                hidden
              />
            </div>

            {/* File List */}
            {files.length > 0 && (
              <div className="file-list">
                <h3>Selected Files ({files.length})</h3>
                {files.map((f, i) => (
                  <div key={i} className="file-item">
                    <div className="file-info">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="file-icon">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                        <polyline points="14 2 14 8 20 8" />
                      </svg>
                      <span className="file-name">{f.name}</span>
                      <span className="file-size">{formatFileSize(f.size)}</span>
                    </div>
                    <button className="remove-btn" onClick={() => removeFile(i)}>
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <line x1="18" y1="6" x2="6" y2="18" />
                        <line x1="6" y1="6" x2="18" y2="18" />
                      </svg>
                    </button>
                  </div>
                ))}
                <div className="page-limit">
                  <label htmlFor="maxPages">Max pages per PDF</label>
                  <input
                    id="maxPages"
                    type="number"
                    min="1"
                    placeholder="All"
                    value={maxPages}
                    onChange={(e) => setMaxPages(e.target.value)}
                  />
                </div>
                <button className="process-btn" onClick={startProcessing}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <polygon points="5 3 19 12 5 21 5 3" />
                  </svg>
                  Start Processing
                </button>
              </div>
            )}
          </div>
        )}

        {/* Processing View */}
        {(status === 'uploading' || status === 'processing') && (
          <div className="processing-section">
            <div className="processing-card">
              <div className="spinner" />
              <h2>Processing...</h2>
              <p className="stage-text">{stage}</p>

              <div className="progress-container">
                <div className="progress-bar">
                  <div
                    className="progress-fill"
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <span className="progress-text">{progress}%</span>
              </div>

              {detail && <p className="detail-text">{detail}</p>}

              <div className="file-tags">
                {files.map((f, i) => (
                  <span key={i} className="file-tag">{f.name}</span>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Completed View */}
        {status === 'completed' && (
          <div className="result-section">
            <div className="result-card success">
              <div className="result-icon success-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <h2>Processing Complete!</h2>
              <p className="detail-text">{detail}</p>
              <div className="result-actions">
                <button className="download-btn" onClick={downloadResult}>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <polyline points="7 10 12 15 17 10" />
                    <line x1="12" y1="15" x2="12" y2="3" />
                  </svg>
                  Download Excel
                </button>
                <button className="reset-btn" onClick={resetAll}>
                  Process More Files
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Error View */}
        {status === 'failed' && (
          <div className="result-section">
            <div className="result-card error">
              <div className="result-icon error-icon">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </div>
              <h2>Processing Failed</h2>
              <p className="error-text">{error}</p>
              <button className="reset-btn" onClick={resetAll}>
                Try Again
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

export default App
