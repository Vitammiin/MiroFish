"""
图谱相关API路由
采用项目上下文机制，服务端持久化状态
"""

import os
import re
import traceback
import threading
from datetime import datetime
from flask import request, jsonify

from . import graph_bp
from ..config import Config
from ..services.ontology_generator import OntologyGenerator
from ..services.graph_builder import GraphBuilderService
from ..services.text_processor import TextProcessor
from ..utils.file_parser import FileParser
from ..utils.logger import get_logger
from ..utils.locale import t, get_locale, set_locale
from ..utils.zep_errors import (
    is_zep_rate_limit_error,
    is_zep_usage_limit_error,
    extract_retry_after_seconds,
    build_zep_rate_limit_message,
    build_zep_usage_limit_message,
)
from ..models.task import TaskManager, TaskStatus
from ..models.project import ProjectManager, ProjectStatus

# 获取日志器
logger = get_logger('mirofish.api')


def _load_cached_graph_snapshot(graph_id: str):
    """按 graph_id 读取本地缓存的图谱快照。"""
    project = ProjectManager.find_project_by_graph_id(graph_id)
    if not project:
        return None, None
    return project, ProjectManager.get_graph_snapshot(project.project_id)


def _resolve_graph_build_config(project, graph_name=None, chunk_size=None, chunk_overlap=None):
    """Resolve graph build options from request input or the persisted project config."""
    return (
        graph_name or project.name or 'MiroFish Graph',
        chunk_size if chunk_size is not None else (project.chunk_size or Config.DEFAULT_CHUNK_SIZE),
        chunk_overlap if chunk_overlap is not None else (project.chunk_overlap or Config.DEFAULT_CHUNK_OVERLAP),
    )


def _slugify_preview_token(value: str, fallback: str) -> str:
    token = re.sub(r'[^a-z0-9]+', '-', (value or '').strip().lower()).strip('-')
    return token or fallback


def _build_local_preview_graph_data(project):
    """Build a lightweight preview graph directly from the generated ontology."""
    ontology = project.ontology or {}
    entity_defs = ontology.get("entity_types", []) or []
    edge_defs = ontology.get("edge_types", []) or []
    now = datetime.now().isoformat()
    graph_id = f"local_preview_{project.project_id}"

    nodes = []
    node_map = {}
    for index, entity in enumerate(entity_defs):
        entity_name = entity.get("name") or f"Entity{index + 1}"
        node_uuid = f"preview-node-{_slugify_preview_token(entity_name, f'entity-{index + 1}')}"
        attributes = {}

        for attr_index, attr in enumerate(entity.get("attributes", []) or []):
            attr_name = attr.get("name") or f"attr_{attr_index + 1}"
            attr_type = attr.get("type") or "text"
            attr_desc = attr.get("description") or ""
            attributes[attr_name] = f"{attr_type}: {attr_desc}".strip(": ")

        for example_index, example in enumerate(entity.get("examples", []) or []):
            attributes[f"example_{example_index + 1}"] = example

        node = {
            "uuid": node_uuid,
            "name": entity_name,
            "labels": ["Entity", entity_name],
            "summary": entity.get("description") or "",
            "attributes": attributes,
            "created_at": now,
        }
        nodes.append(node)
        node_map[entity_name] = node

    edges = []
    edge_index = 0
    for edge_def in edge_defs:
        edge_name = edge_def.get("name") or f"EDGE_{edge_index + 1}"
        description = edge_def.get("description") or ""
        for source_target in edge_def.get("source_targets", []) or []:
            source_name = source_target.get("source")
            target_name = source_target.get("target")
            source_node = node_map.get(source_name)
            target_node = node_map.get(target_name)
            if not source_node or not target_node:
                continue

            edge_index += 1
            edges.append({
                "uuid": f"preview-edge-{_slugify_preview_token(edge_name, f'edge-{edge_index}')}-{edge_index}",
                "name": edge_name,
                "fact": description,
                "fact_type": edge_name,
                "source_node_uuid": source_node["uuid"],
                "target_node_uuid": target_node["uuid"],
                "source_node_name": source_name,
                "target_node_name": target_name,
                "attributes": {},
                "created_at": now,
                "valid_at": now,
                "invalid_at": None,
                "expired_at": None,
                "episodes": [],
            })

    return {
        "graph_id": graph_id,
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


def _activate_local_preview_graph(project, warning_message: str):
    """Persist a local ontology-based preview graph for degraded mode."""
    graph_data = _build_local_preview_graph_data(project)
    project.status = ProjectStatus.GRAPH_COMPLETED
    project.graph_id = graph_data["graph_id"]
    project.graph_source = "local_preview"
    project.graph_warning = warning_message
    project.graph_build_task_id = None
    project.error = None
    ProjectManager.save_project(project)
    ProjectManager.save_graph_snapshot(
        project.project_id,
        graph_data,
        meta={
            "graph_source": "local_preview",
            "graph_warning": warning_message,
        },
    )
    return graph_data


def _start_graph_build_task(
    project,
    *,
    graph_name=None,
    chunk_size=None,
    chunk_overlap=None,
    reset_graph=False,
    recovery_reason=None,
):
    """Create and launch a graph build task for the given project."""
    graph_name, chunk_size, chunk_overlap = _resolve_graph_build_config(
        project,
        graph_name=graph_name,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )

    text = ProjectManager.get_extracted_text(project.project_id)
    if not text:
        raise ValueError(t('api.textNotFound'))

    ontology = project.ontology
    if not ontology:
        raise ValueError(t('api.ontologyNotFound'))

    if reset_graph:
        project.graph_id = None

    project.chunk_size = chunk_size
    project.chunk_overlap = chunk_overlap
    project.error = None
    project.graph_source = None
    project.graph_warning = None

    task_manager = TaskManager()
    metadata = {"project_id": project.project_id}
    if recovery_reason:
        metadata["recovery_reason"] = recovery_reason

    task_id = task_manager.create_task(f"构建图谱: {graph_name}", metadata=metadata)
    logger.info(
        f"创建图谱构建任务: task_id={task_id}, project_id={project.project_id}, recovery={bool(recovery_reason)}"
    )

    project.status = ProjectStatus.GRAPH_BUILDING
    project.graph_build_task_id = task_id
    ProjectManager.save_project(project)

    current_locale = get_locale()
    project_id = project.project_id

    def build_task():
        set_locale(current_locale)
        build_logger = get_logger('mirofish.build')

        try:
            build_logger.info(f"[{task_id}] 开始构建图谱...")
            initial_message = t('progress.initGraphService')
            if recovery_reason:
                initial_message = f"{initial_message} (restored after backend restart)"

            task_manager.update_task(
                task_id,
                status=TaskStatus.PROCESSING,
                message=initial_message,
                progress=1 if recovery_reason else None,
            )

            builder = GraphBuilderService(api_key=Config.ZEP_API_KEY)

            task_manager.update_task(
                task_id,
                message=t('progress.textChunking'),
                progress=5,
            )
            chunks = TextProcessor.split_text(
                text,
                chunk_size=chunk_size,
                overlap=chunk_overlap,
            )
            total_chunks = len(chunks)

            task_manager.update_task(
                task_id,
                message=t('progress.creatingZepGraph'),
                progress=10,
            )
            graph_id = builder.create_graph(name=graph_name)

            project_state = ProjectManager.get_project(project_id) or project
            project_state.graph_id = graph_id
            project_state.error = None
            project_state.graph_source = "zep"
            project_state.graph_warning = None
            ProjectManager.save_project(project_state)

            task_manager.update_task(
                task_id,
                message=t('progress.settingOntology'),
                progress=15,
            )
            builder.set_ontology(graph_id, ontology)

            def add_progress_callback(msg, progress_ratio):
                progress = 15 + int(progress_ratio * 40)
                task_manager.update_task(
                    task_id,
                    message=msg,
                    progress=progress,
                )

            task_manager.update_task(
                task_id,
                message=t('progress.addingChunks', count=total_chunks),
                progress=15,
            )

            episode_uuids = builder.add_text_batches(
                graph_id,
                chunks,
                batch_size=3,
                progress_callback=add_progress_callback,
            )

            task_manager.update_task(
                task_id,
                message=t('progress.waitingZepProcess'),
                progress=55,
            )

            def wait_progress_callback(msg, progress_ratio):
                progress = 55 + int(progress_ratio * 35)
                task_manager.update_task(
                    task_id,
                    message=msg,
                    progress=progress,
                )

            builder._wait_for_episodes(episode_uuids, wait_progress_callback)

            task_manager.update_task(
                task_id,
                message=t('progress.fetchingGraphData'),
                progress=95,
            )

            try:
                graph_data = builder.get_graph_data(graph_id)
                ProjectManager.save_graph_snapshot(
                    project_id,
                    graph_data,
                    meta={"graph_source": "zep"},
                )
            except Exception as e:
                if is_zep_rate_limit_error(e):
                    retry_after = extract_retry_after_seconds(e)
                    build_logger.warning(build_zep_rate_limit_message(retry_after, using_cache=False))
                    cached_snapshot = ProjectManager.get_graph_snapshot(project_id)
                    graph_data = cached_snapshot.get("graph_data", {}) if cached_snapshot else {
                        "graph_id": graph_id,
                        "nodes": [],
                        "edges": [],
                        "node_count": 0,
                        "edge_count": 0,
                    }
                elif is_zep_usage_limit_error(e):
                    warning_message = build_zep_usage_limit_message(using_local_preview=True)
                    build_logger.warning(
                        f"{warning_message}. Falling back to local ontology preview."
                    )
                    project_state = ProjectManager.get_project(project_id) or project
                    graph_data = _activate_local_preview_graph(project_state, warning_message)
                else:
                    raise

            project_state = ProjectManager.get_project(project_id) or project
            project_state.status = ProjectStatus.GRAPH_COMPLETED
            project_state.error = None
            project_state.graph_build_task_id = task_id
            if project_state.graph_source != "local_preview":
                project_state.graph_source = "zep"
                project_state.graph_warning = None
            ProjectManager.save_project(project_state)

            node_count = graph_data.get("node_count", 0)
            edge_count = graph_data.get("edge_count", 0)
            build_logger.info(
                f"[{task_id}] 图谱构建完成: graph_id={graph_id}, 节点={node_count}, 边={edge_count}"
            )

            task_manager.update_task(
                task_id,
                status=TaskStatus.COMPLETED,
                message=t('progress.graphBuildComplete'),
                progress=100,
                result={
                    "project_id": project_id,
                    "graph_id": graph_data.get("graph_id", graph_id),
                    "node_count": node_count,
                    "edge_count": edge_count,
                    "chunk_count": total_chunks,
                },
            )

        except Exception as e:
            if is_zep_usage_limit_error(e):
                warning_message = build_zep_usage_limit_message(using_local_preview=True)
                build_logger.warning(
                    f"[{task_id}] Zep usage limit reached, switching to local preview graph."
                )

                project_state = ProjectManager.get_project(project_id) or project
                graph_data = _activate_local_preview_graph(project_state, warning_message)
                node_count = graph_data.get("node_count", 0)
                edge_count = graph_data.get("edge_count", 0)

                task_manager.update_task(
                    task_id,
                    status=TaskStatus.COMPLETED,
                    message=warning_message,
                    progress=100,
                    result={
                        "project_id": project_id,
                        "graph_id": graph_data.get("graph_id"),
                        "node_count": node_count,
                        "edge_count": edge_count,
                        "chunk_count": 0,
                        "local_preview": True,
                        "warning": warning_message,
                    },
                )
                return

            build_logger.error(f"[{task_id}] 图谱构建失败: {str(e)}")
            build_logger.debug(traceback.format_exc())

            project_state = ProjectManager.get_project(project_id) or project
            project_state.status = ProjectStatus.FAILED
            project_state.error = str(e)
            ProjectManager.save_project(project_state)

            task_manager.update_task(
                task_id,
                status=TaskStatus.FAILED,
                message=t('progress.buildFailed', error=str(e)),
                error=traceback.format_exc(),
            )

    thread = threading.Thread(target=build_task, daemon=True)
    thread.start()

    return task_id


def _recover_interrupted_graph_build(project):
    """Restore a build after backend restart by launching a fresh replacement task."""
    if project.status != ProjectStatus.GRAPH_BUILDING:
        return project, None

    active_task_id = project.graph_build_task_id
    if active_task_id and TaskManager().get_task(active_task_id):
        return project, active_task_id

    stale_task_id = active_task_id or 'unknown'
    logger.warning(
        f"检测到中断的图谱构建任务: project_id={project.project_id}, stale_task_id={stale_task_id}. 启动恢复任务。"
    )

    recovered_task_id = _start_graph_build_task(
        project,
        graph_name=project.name or 'MiroFish Graph',
        chunk_size=project.chunk_size or Config.DEFAULT_CHUNK_SIZE,
        chunk_overlap=project.chunk_overlap or Config.DEFAULT_CHUNK_OVERLAP,
        reset_graph=True,
        recovery_reason='backend_restart',
    )

    recovered_project = ProjectManager.get_project(project.project_id) or project
    return recovered_project, recovered_task_id


def _recover_failed_usage_limited_project(project):
    """Convert failed quota-limited builds into a local preview graph."""
    if project.status != ProjectStatus.FAILED:
        return project, False
    if not project.ontology:
        return project, False
    if not is_zep_usage_limit_error(project.error or ""):
        return project, False

    warning_message = build_zep_usage_limit_message(using_local_preview=True)
    logger.warning(
        f"检测到 Zep usage limit 失败项目，切换到本地 preview 图谱: project_id={project.project_id}"
    )
    preview_project = ProjectManager.get_project(project.project_id) or project
    _activate_local_preview_graph(preview_project, warning_message)
    recovered_project = ProjectManager.get_project(project.project_id) or preview_project
    return recovered_project, True


def allowed_file(filename: str) -> bool:
    """检查文件扩展名是否允许"""
    if not filename or '.' not in filename:
        return False
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    return ext in Config.ALLOWED_EXTENSIONS


# ============== 项目管理接口 ==============

@graph_bp.route('/project/<project_id>', methods=['GET'])
def get_project(project_id: str):
    """
    获取项目详情
    """
    project = ProjectManager.get_project(project_id)
    
    if not project:
        return jsonify({
            "success": False,
            "error": t('api.projectNotFound', id=project_id)
        }), 404

    recovered_task_id = None
    usage_limit_recovered = False
    if project.status == ProjectStatus.GRAPH_BUILDING:
        project, recovered_task_id = _recover_interrupted_graph_build(project)
    else:
        project, usage_limit_recovered = _recover_failed_usage_limited_project(project)

    response = {
        "success": True,
        "data": project.to_dict()
    }
    if recovered_task_id and recovered_task_id != project.graph_build_task_id:
        response["data"]["graph_build_task_id"] = recovered_task_id
    if recovered_task_id:
        response["meta"] = {
            "graph_build_recovered": True,
            "graph_build_task_id": recovered_task_id,
        }
    if usage_limit_recovered:
        response.setdefault("meta", {})
        response["meta"]["local_preview_recovered"] = True

    return jsonify(response)


@graph_bp.route('/project/list', methods=['GET'])
def list_projects():
    """
    列出所有项目
    """
    limit = request.args.get('limit', 50, type=int)
    projects = ProjectManager.list_projects(limit=limit)
    
    return jsonify({
        "success": True,
        "data": [p.to_dict() for p in projects],
        "count": len(projects)
    })


@graph_bp.route('/project/<project_id>', methods=['DELETE'])
def delete_project(project_id: str):
    """
    删除项目
    """
    success = ProjectManager.delete_project(project_id)
    
    if not success:
        return jsonify({
            "success": False,
            "error": t('api.projectDeleteFailed', id=project_id)
        }), 404

    return jsonify({
        "success": True,
        "message": t('api.projectDeleted', id=project_id)
    })


@graph_bp.route('/project/<project_id>/reset', methods=['POST'])
def reset_project(project_id: str):
    """
    重置项目状态（用于重新构建图谱）
    """
    project = ProjectManager.get_project(project_id)
    
    if not project:
        return jsonify({
            "success": False,
            "error": t('api.projectNotFound', id=project_id)
        }), 404

    # 重置到本体已生成状态
    if project.ontology:
        project.status = ProjectStatus.ONTOLOGY_GENERATED
    else:
        project.status = ProjectStatus.CREATED
    
    project.graph_id = None
    project.graph_build_task_id = None
    project.graph_source = None
    project.graph_warning = None
    project.error = None
    ProjectManager.save_project(project)
    
    return jsonify({
        "success": True,
        "message": t('api.projectReset', id=project_id),
        "data": project.to_dict()
    })


# ============== 接口1：上传文件并生成本体 ==============

@graph_bp.route('/ontology/generate', methods=['POST'])
def generate_ontology():
    """
    接口1：上传文件，分析生成本体定义
    
    请求方式：multipart/form-data
    
    参数：
        files: 上传的文件（PDF/MD/TXT），可多个
        simulation_requirement: 模拟需求描述（必填）
        project_name: 项目名称（可选）
        additional_context: 额外说明（可选）
        
    返回：
        {
            "success": true,
            "data": {
                "project_id": "proj_xxxx",
                "ontology": {
                    "entity_types": [...],
                    "edge_types": [...],
                    "analysis_summary": "..."
                },
                "files": [...],
                "total_text_length": 12345
            }
        }
    """
    try:
        logger.info("=== 开始生成本体定义 ===")

        llm_errors = Config.validate_llm()
        if llm_errors:
            logger.error(f"配置错误: {llm_errors}")
            return jsonify({
                "success": False,
                "error": t('api.configError', details="; ".join(llm_errors))
            }), 500
        
        # 获取参数
        simulation_requirement = request.form.get('simulation_requirement', '')
        project_name = request.form.get('project_name', 'Unnamed Project')
        additional_context = request.form.get('additional_context', '')
        
        logger.debug(f"项目名称: {project_name}")
        logger.debug(f"模拟需求: {simulation_requirement[:100]}...")
        
        if not simulation_requirement:
            return jsonify({
                "success": False,
                "error": t('api.requireSimulationRequirement')
            }), 400
        
        # 获取上传的文件
        uploaded_files = request.files.getlist('files')
        if not uploaded_files or all(not f.filename for f in uploaded_files):
            return jsonify({
                "success": False,
                "error": t('api.requireFileUpload')
            }), 400
        
        # 创建项目
        project = ProjectManager.create_project(name=project_name)
        project.simulation_requirement = simulation_requirement
        logger.info(f"创建项目: {project.project_id}")
        
        # 保存文件并提取文本
        document_texts = []
        all_text = ""
        
        for file in uploaded_files:
            if file and file.filename and allowed_file(file.filename):
                # 保存文件到项目目录
                file_info = ProjectManager.save_file_to_project(
                    project.project_id, 
                    file, 
                    file.filename
                )
                project.files.append({
                    "filename": file_info["original_filename"],
                    "size": file_info["size"]
                })
                
                # 提取文本
                text = FileParser.extract_text(file_info["path"])
                text = TextProcessor.preprocess_text(text)
                document_texts.append(text)
                all_text += f"\n\n=== {file_info['original_filename']} ===\n{text}"
        
        if not document_texts:
            ProjectManager.delete_project(project.project_id)
            return jsonify({
                "success": False,
                "error": t('api.noDocProcessed')
            }), 400
        
        # 保存提取的文本
        project.total_text_length = len(all_text)
        ProjectManager.save_extracted_text(project.project_id, all_text)
        logger.info(f"文本提取完成，共 {len(all_text)} 字符")
        
        # 生成本体
        logger.info("调用 LLM 生成本体定义...")
        generator = OntologyGenerator()
        ontology = generator.generate(
            document_texts=document_texts,
            simulation_requirement=simulation_requirement,
            additional_context=additional_context if additional_context else None
        )
        
        # 保存本体到项目
        entity_count = len(ontology.get("entity_types", []))
        edge_count = len(ontology.get("edge_types", []))
        logger.info(f"本体生成完成: {entity_count} 个实体类型, {edge_count} 个关系类型")
        
        project.ontology = {
            "entity_types": ontology.get("entity_types", []),
            "edge_types": ontology.get("edge_types", [])
        }
        project.analysis_summary = ontology.get("analysis_summary", "")
        project.status = ProjectStatus.ONTOLOGY_GENERATED
        ProjectManager.save_project(project)
        logger.info(f"=== 本体生成完成 === 项目ID: {project.project_id}")
        
        return jsonify({
            "success": True,
            "data": {
                "project_id": project.project_id,
                "project_name": project.name,
                "ontology": project.ontology,
                "analysis_summary": project.analysis_summary,
                "files": project.files,
                "total_text_length": project.total_text_length
            }
        })
        
    except Exception as e:
        logger.exception("生成本体定义失败")
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== 接口2：构建图谱 ==============

@graph_bp.route('/build', methods=['POST'])
def build_graph():
    """
    接口2：根据project_id构建图谱
    
    请求（JSON）：
        {
            "project_id": "proj_xxxx",  // 必填，来自接口1
            "graph_name": "图谱名称",    // 可选
            "chunk_size": 500,          // 可选，默认500
            "chunk_overlap": 50         // 可选，默认50
        }
        
    返回：
        {
            "success": true,
            "data": {
                "project_id": "proj_xxxx",
                "task_id": "task_xxxx",
                "message": "图谱构建任务已启动"
            }
        }
    """
    try:
        logger.info("=== 开始构建图谱 ===")
        
        # 检查配置
        errors = []
        if not Config.ZEP_API_KEY:
            errors.append(t('api.zepApiKeyMissing'))
        if errors:
            logger.error(f"配置错误: {errors}")
            return jsonify({
                "success": False,
                "error": t('api.configError', details="; ".join(errors))
            }), 500
        
        # 解析请求
        data = request.get_json() or {}
        project_id = data.get('project_id')
        logger.debug(f"请求参数: project_id={project_id}")
        
        if not project_id:
            return jsonify({
                "success": False,
                "error": t('api.requireProjectId')
            }), 400
        
        # 获取项目
        project = ProjectManager.get_project(project_id)
        if not project:
            return jsonify({
                "success": False,
                "error": t('api.projectNotFound', id=project_id)
            }), 404

        # 检查项目状态
        force = data.get('force', False)

        if project.status == ProjectStatus.CREATED:
            return jsonify({
                "success": False,
                "error": t('api.ontologyNotGenerated')
            }), 400

        if project.status == ProjectStatus.GRAPH_BUILDING and not force:
            active_task_id = project.graph_build_task_id
            if active_task_id and TaskManager().get_task(active_task_id):
                return jsonify({
                    "success": False,
                    "error": t('api.graphBuilding'),
                    "task_id": active_task_id
                }), 400

            project, task_id = _recover_interrupted_graph_build(project)
            return jsonify({
                "success": True,
                "data": {
                    "project_id": project_id,
                    "task_id": task_id,
                    "message": "Graph build was restored after backend restart.",
                    "recovered": True
                }
            })

        reset_graph = force and project.status in [
            ProjectStatus.GRAPH_BUILDING,
            ProjectStatus.FAILED,
            ProjectStatus.GRAPH_COMPLETED,
        ]
        if force:
            project.status = ProjectStatus.ONTOLOGY_GENERATED
            project.graph_build_task_id = None
            project.error = None

        task_id = _start_graph_build_task(
            project,
            graph_name=data.get('graph_name'),
            chunk_size=data.get('chunk_size'),
            chunk_overlap=data.get('chunk_overlap'),
            reset_graph=reset_graph,
        )

        return jsonify({
            "success": True,
            "data": {
                "project_id": project_id,
                "task_id": task_id,
                "message": t('api.graphBuildStarted', taskId=task_id)
            }
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


# ============== 任务查询接口 ==============

@graph_bp.route('/task/<task_id>', methods=['GET'])
def get_task(task_id: str):
    """
    查询任务状态
    """
    task = TaskManager().get_task(task_id)
    
    if not task:
        return jsonify({
            "success": False,
            "error": t('api.taskNotFound', id=task_id)
        }), 404
    
    return jsonify({
        "success": True,
        "data": task.to_dict()
    })


@graph_bp.route('/tasks', methods=['GET'])
def list_tasks():
    """
    列出所有任务
    """
    tasks = TaskManager().list_tasks()
    
    return jsonify({
        "success": True,
        "data": [t.to_dict() for t in tasks],
        "count": len(tasks)
    })


# ============== 图谱数据接口 ==============

@graph_bp.route('/data/<graph_id>', methods=['GET'])
def get_graph_data(graph_id: str):
    """
    获取图谱数据（节点和边）
    """
    try:
        project, cached_snapshot = _load_cached_graph_snapshot(graph_id)
        snapshot_meta = cached_snapshot.get("meta", {}) if cached_snapshot else {}

        if cached_snapshot and snapshot_meta.get("graph_source") == "local_preview":
            return jsonify({
                "success": True,
                "data": cached_snapshot.get("graph_data", {}),
                "warning": snapshot_meta.get("graph_warning"),
                "meta": {
                    "from_cache": True,
                    "local_preview": True,
                    "cached_at": cached_snapshot.get("cached_at")
                }
            })

        if not Config.ZEP_API_KEY:
            if cached_snapshot:
                return jsonify({
                    "success": True,
                    "data": cached_snapshot.get("graph_data", {}),
                    "warning": "ZEP_API_KEY missing, serving cached graph snapshot.",
                    "meta": {
                        "from_cache": True,
                        "cached_at": cached_snapshot.get("cached_at")
                    }
                })
            return jsonify({
                "success": False,
                "error": t('api.zepApiKeyMissing')
            }), 500
        
        builder = GraphBuilderService(api_key=Config.ZEP_API_KEY)
        try:
            graph_data = builder.get_graph_data(graph_id)
        except Exception as e:
            if is_zep_rate_limit_error(e):
                retry_after = extract_retry_after_seconds(e)
                if cached_snapshot:
                    logger.warning(
                        f"Zep rate limit while loading graph {graph_id}; serving cached snapshot instead."
                    )
                    return jsonify({
                        "success": True,
                        "data": cached_snapshot.get("graph_data", {}),
                        "warning": build_zep_rate_limit_message(retry_after, using_cache=True),
                        "meta": {
                            "from_cache": True,
                            "cached_at": cached_snapshot.get("cached_at"),
                            "retry_after": retry_after
                        }
                    })
                return jsonify({
                    "success": False,
                    "error": build_zep_rate_limit_message(retry_after, using_cache=False),
                    "details": str(e)
                }), 429
            raise

        if project:
            ProjectManager.save_graph_snapshot(
                project.project_id,
                graph_data,
                meta={"graph_source": "zep"},
            )
        
        return jsonify({
            "success": True,
            "data": graph_data,
            "meta": {
                "from_cache": False
            }
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500


@graph_bp.route('/delete/<graph_id>', methods=['DELETE'])
def delete_graph(graph_id: str):
    """
    删除Zep图谱
    """
    try:
        if not Config.ZEP_API_KEY:
            return jsonify({
                "success": False,
                "error": t('api.zepApiKeyMissing')
            }), 500
        
        builder = GraphBuilderService(api_key=Config.ZEP_API_KEY)
        builder.delete_graph(graph_id)
        
        return jsonify({
            "success": True,
            "message": t('api.graphDeleted', id=graph_id)
        })
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }), 500
