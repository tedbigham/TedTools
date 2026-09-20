bl_info = {
    "name": "Ted Tools",
    "author": "Ted Bigham",
    "version": (2,3,0),
    "blender": (4, 5, 0),
    "location": "3D View > N‑Panel > Ted",
    "description": "Assortment of technical tools.",
    "category": "Mesh",
}

__version__ = bl_info["version"]
__version_str__ = ".".join(str(x) for x in __version__)

import bpy, bmesh, math, colorsys
import hashlib
import os
import re
import shutil
import tempfile
import time
from array import array
from contextlib import contextmanager
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree
from collections import deque, defaultdict

# ---------------------------------------------------------------------------
# Utilities / layers
# ---------------------------------------------------------------------------

def ensure_layers(me, bm):
    fa_id = bm.faces.layers.int.get("ClusterId") or bm.faces.layers.int.new("ClusterId")
    fa_vis = bm.faces.layers.int.get("FumesVisited") or bm.faces.layers.int.new("FumesVisited")
    cl = bm.loops.layers.color.get("ClusterPreview")
    if not cl:
        cl = bm.loops.layers.color.new("ClusterPreview")
        try:
            me.color_attributes.active_color  = me.color_attributes["ClusterPreview"]
            me.color_attributes.active_render = me.color_attributes["ClusterPreview"]
        except Exception:
            pass
    return cl, fa_id, fa_vis

def cleanup_layers(bm):
    fa_vis = bm.faces.layers.int.get("FumesVisited")
    if fa_vis:
        bm.faces.layers.int.remove(fa_vis)

def _palette(i:int):
    import colorsys
    h=(i*0.618034)%1.0
    r,g,b = colorsys.hsv_to_rgb(h, 0.55, 1.0)
    return (r,g,b,1.0)

def colorize_clusters(bm, cl, fa_id, clusters):
    for cid, faces in enumerate(clusters):
        col=_palette(cid)
        for f in faces:
            f[fa_id]=cid
            for lp in f.loops: lp[cl]=col

def recolor_by_id(bm, cl, fa_id):
    for f in bm.faces:
        cid=f[fa_id]
        col=_palette(cid) if cid>=0 else (0.1,0.1,0.1,1.0)
        for lp in f.loops: lp[cl]=col

def build_neighbors(bm):
    nbrs=[set() for _ in bm.faces]
    for e in bm.edges:
        if len(e.link_faces)==2:
            a,b=e.link_faces[0],e.link_faces[1]
            nbrs[a.index].add(b)
            nbrs[b.index].add(a)
    return nbrs

def next_cluster_id(bm, fa_id):
    m=-1
    for f in bm.faces:
        cid=f[fa_id]
        if cid is not None and cid>m: m=cid
    return m+1

# robust vector helpers
_EPS = 1e-16
def _valid_normal(n: Vector) -> bool:
    return n.length_squared > _EPS

def _safe_angle(a: Vector, b: Vector) -> float:
    if a.length_squared <= _EPS or b.length_squared <= _EPS: return math.pi
    da=a.length
    db=b.length
    c=max(-1.0, min(1.0, a.dot(b)/(da*db)))
    return math.acos(c)

def filter_mesh_to_selected(bm):
    bm_new = bmesh.new()
    vert_map = {}
    has_select = False
    for f in bm.faces:
        if f.select:
            has_select = True
            new_verts = []
            for v in f.verts:
                if v not in vert_map:
                    vert_map[v] = bm_new.verts.new(v.co)
                new_verts.append(vert_map[v])
            bm_new.faces.new(new_verts)

    bm_new.verts.index_update()
    bm_new.faces.index_update()
    bm_new.faces.ensure_lookup_table()
    bm_new.edges.ensure_lookup_table()
    bm_new.normal_update()
    
    return bm_new if has_select == True else bm

# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class FumesClusterSettings(bpy.types.PropertyGroup):
    min_faces:     bpy.props.IntProperty  (name="Min Faces (strict)", default=4, min=1)
    angle_start:   bpy.props.FloatProperty(name="Initial Angle ≤ °",  default=math.radians(8.0),  subtype='ANGLE')
    angle_grow:    bpy.props.FloatProperty(name="Grow Angle ≤ °",     default=math.radians(8.0), subtype='ANGLE')
    prune_leaves:  bpy.props.BoolProperty (name="Prune Leaves",       default=False)

# ---------------------------------------------------------------------------
# Surface Clustering
# ---------------------------------------------------------------------------

class MESH_OT_assign_cluster_ids(bpy.types.Operator):
    bl_idname="mesh.assign_cluster_ids"
    bl_label ="Assign Surface Cluster Ids"
    bl_options={'REGISTER','UNDO'}
    bl_description="Detect clusters and assign unique attribute (ClusterId) to each one."

    def execute(self, ctx):
        if bpy.context.mode!='EDIT_MESH':
            self.report({'ERROR'}, "Edit Mode → Face Select")
            return {'CANCELLED'}
        obj=ctx.object
        if not obj or obj.type!='MESH':
            self.report({'ERROR'}, "Select a mesh")
            return {'CANCELLED'}
        p=ctx.scene.fumes_cluster
        me=obj.data
        bm=bmesh.from_edit_mesh(me)
        
        cl, fa_id, fa_vis = ensure_layers(me, bm)
        nbrs = build_neighbors(bm)

        select_all = all(f.select == False for f in bm.faces)
        cand=[f for f in bm.faces if _valid_normal(f.normal) and (select_all or f.select)]
        print(f"candidate faces:{len(cand)}")
        if not cand:
            self.report({'ERROR'}, "No candidate faces")
            return {'CANCELLED'}
        cand_set=set(cand)
        nocolor = (0.0,0.0,0.0,1.0)#_palette(-1)
        for f in cand:
            f[fa_id]=-1
            f[fa_vis]=0
            for lp in f.loops:
                lp[cl]=nocolor

        def strict_accepts(candidate, strict_list):
            cn=candidate.normal
            if not _valid_normal(cn):
                return False
            for f in strict_list:
                if not _valid_normal(f.normal):
                    return False
                if _safe_angle(f.normal, cn) > p.angle_start:
                    return False
            return True

        deg_in={f.index: sum(1 for n in nbrs[f.index] if n in cand_set) for f in cand}
        seeds=sorted(cand, key=lambda f:deg_in[f.index], reverse=True)

        clusters=[]
        for seed in seeds:
            if seed[fa_vis]==1:
                continue
            strict_list=[seed]
            dq=deque([seed])
            seed[fa_vis]=1
            while dq:
                f=dq.popleft()
                for fn in nbrs[f.index]:
                    if fn[fa_vis]==1 or fn not in cand_set:
                        continue
                    if strict_accepts(fn, strict_list):
                        strict_list.append(fn)
                        fn[fa_vis]=1
                        dq.append(fn)
            if len(strict_list) < p.min_faces:
                continue

            grown=set(strict_list)
            dq=deque(strict_list)
            while dq:
                f=dq.popleft()
                nf=f.normal
                if not _valid_normal(nf):
                    continue
                for fn in nbrs[f.index]:
                    if fn in grown or fn not in cand_set:
                        continue
                    if _safe_angle(nf, fn.normal) <= p.angle_grow:
                        grown.add(fn)
                        dq.append(fn)

            # prune leaves
            C={f.index for f in grown}
            if p.prune_leaves:
                q=deque(C)
                while q:
                    i=q.popleft()
                    if i not in C:
                        continue
                    neigh=[n.index for n in nbrs[i] if n.index in C]
                    if len(neigh)==1:
                        C.remove(i)
                        q.append(neigh[0])

            core=[bm.faces[i] for i in C]
            if len(core)<p.min_faces:
                for f in strict_list:
                    f[fa_vis]=0
                continue
            clusters.append(core)
            for f in core:
                f[fa_vis]=1

        if not clusters:
            self.report({'INFO'}, "No clusters found")
            bmesh.update_edit_mesh(me, loop_triangles=False)
            return {'CANCELLED'}

        colorize_clusters(bm, cl, fa_id, clusters)    
        bmesh.update_edit_mesh(me, loop_triangles=False)
        cleanup_layers(bm)
        
        self.report({'INFO'}, f"Clusters: {len(clusters)}")
        return {'FINISHED'}


class MESH_OT_expand_selection(bpy.types.Operator):
    bl_idname="mesh.select_surface_cluster"
    bl_label="Expand Selection"
    bl_options={'REGISTER','UNDO'}
    bl_description="Adds connected faces within Grow Angle to selected faces"

    def execute(self, ctx):
        if bpy.context.mode!='EDIT_MESH':
            self.report({'ERROR'}, "Edit Mode → Face Select")
            return {'CANCELLED'}
        obj=ctx.object
        if not obj or obj.type!='MESH':
            self.report({'ERROR'}, "Select a mesh")
            return {'CANCELLED'}
        p=ctx.scene.fumes_cluster
        me=obj.data
        bm=bmesh.from_edit_mesh(me)
        
        cl, fa_id, fa_vis = ensure_layers(me, bm)
        nbrs = build_neighbors(bm)

        cand=[f for f in bm.faces if _valid_normal(f.normal)]
        cand_set=set(cand)

        for f in cand:
            f[fa_vis]=0

        def strict_accepts(candidate, strict_list):
            cn=candidate.normal
            if not _valid_normal(cn):
                return False
            for f in strict_list:
                if not _valid_normal(f.normal):
                    return False
                if _safe_angle(f.normal, cn) > p.angle_start:
                    return False
            return True

        seeds=[f for f in cand if f.select == True]

        for seed in seeds:
            if seed[fa_vis]==1:
                continue
            #seed[fa_vis]=1
            dq=deque([seed])
            grown=set([seed])
            while dq:
                f=dq.popleft()
                if (f[fa_vis]==1):
                    continue
                f[fa_vis]=1
                nf=f.normal
                if not _valid_normal(nf):
                    continue
                for fn in nbrs[f.index]:
                    if fn in grown or fn not in cand_set:
                        continue
                    if _safe_angle(nf, fn.normal) <= p.angle_grow:
                        grown.add(fn)
                        if (fn[fa_vis]==0):
                            dq.append(fn)

            # prune leaves
            C={f.index for f in grown}
            if p.prune_leaves:
                q=deque(C)
                while q:
                    i=q.popleft()
                    if i not in C:
                        continue
                    neigh=[n.index for n in nbrs[i] if n.index in C]
                    if len(neigh)==1:
                        C.remove(i)
                        q.append(neigh[0])

            core=[bm.faces[i] for i in C]
            for f in core:
                f.select = True

        bmesh.update_edit_mesh(me, loop_triangles=False)
        cleanup_layers(bm)
        
        self.report({'INFO'}, f"Selected: {len(C)}")
        return {'FINISHED'}

class MESH_OT_select_faces_by_cluster_id(bpy.types.Operator):
    bl_idname="mesh.select_faces_by_cluster_id"
    bl_label="Select Faces by ClusterId"
    bl_options={'REGISTER','UNDO'}
    bl_description="Select all other faces with the same ClusterId a the active face"

    def execute(self, ctx):
        if bpy.context.mode!='EDIT_MESH':
            self.report({'ERROR'}, "Edit Mode → Face Select")
            return {'CANCELLED'}
        obj=ctx.object
        if not obj or obj.type!='MESH':
            self.report({'ERROR'}, "Select a mesh")
            return {'CANCELLED'}

        mesh=obj.data
        bm=bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bm.edges.ensure_lookup_table()        
        fa_id = bm.faces.layers.int.get("ClusterId")
        if not fa_id:
            self.report({'ERROR'}, "No attribute 'ClusterId'. Did you run 'Find Surface Clusters'?") 
            return {'CANCELLED'}        

        f_ref=bm.select_history.active if isinstance(bm.select_history.active,bmesh.types.BMFace) else None
        if not f_ref:
            f_ref=next((f for f in bm.faces if f.select), None)
            self.report({'INFO'}, f"ref={f_ref}")
        if not f_ref:
            self.report({'ERROR'}, "Select a reference face.") 
            return {'CANCELLED'}
        
        bpy.ops.mesh.select_mode(use_extend=False,use_expand=False,type='FACE')
        for f in bm.faces: f.select = f[fa_id] == f_ref[fa_id] #(dist(face_avg_rgb(f,cl), ref) <= tol)
        bmesh.update_edit_mesh(mesh, loop_triangles=False)
        self.report({'INFO'}, "Selected faces by id.")
        return {'FINISHED'}

class MESH_OT_select_all_cluster_ids(bpy.types.Operator):
    bl_idname="mesh.select_all_cluster_ids"
    bl_label="Select all ClusterIds"
    bl_options={'REGISTER','UNDO'}
    bl_description="Select all, except faces with no ClusterId"

    def execute(self, ctx):
        if bpy.context.mode!='EDIT_MESH':
            self.report({'ERROR'}, "Edit Mode → Face Select")
            return {'CANCELLED'}
        obj=ctx.object
        if not obj or obj.type!='MESH':
            self.report({'ERROR'}, "Select a mesh")
            return {'CANCELLED'}

        mesh=obj.data
        bm=bmesh.from_edit_mesh(mesh)
        bm.faces.ensure_lookup_table()
        bm.edges.ensure_lookup_table()        
        fa_id = bm.faces.layers.int.get("ClusterId")
        if not fa_id:
            self.report({'ERROR'}, "No attribute 'ClusterId'. Did you run 'Find Surface Clusters'?") 
            return {'CANCELLED'}        
       
        bpy.ops.mesh.select_mode(use_extend=False,use_expand=False,type='FACE')
        for f in bm.faces: f.select = f[fa_id] != -1
        bmesh.update_edit_mesh(mesh, loop_triangles=False)
        return {'FINISHED'}

#------------------------------------------------
# Holes
#------------------------------------------------

def follow_boundary_loop(bm, start_edge):
    if not start_edge.is_boundary:
        return []
    loop=[]
    visited=set()
    e=start_edge
    v=e.verts[1]
    start=(e,v)
    while True:
        loop.append(e)
        visited.add(e)
        nxt=None
        for ed in v.link_edges:
            if ed.is_boundary and ed!=e:
                if ed not in visited:
                    nxt=ed
                    break
                nxt=ed
        if nxt is None:
            break
        v=nxt.other_vert(v)
        e=nxt
        if e==start[0] and v in start[0].verts:
            break
        if len(loop)>len(bm.edges):
            break
    return loop

class MESH_OT_select_hole_perimeter(bpy.types.Operator):
    bl_idname="mesh.select_hole_perimeter"
    bl_label="Select Hole Perimeter"
    bl_options={'REGISTER','UNDO'}
    bl_description="Try to select edges to make a single loop around a hole"

    def execute(self, ctx):
        obj=ctx.object
        if not obj or obj.type!='MESH' or bpy.context.mode!='EDIT_MESH':
            self.report({'ERROR'}, "Edit Mesh (Edge Select).")
            return {'CANCELLED'}
        bm=bmesh.from_edit_mesh(obj.data)
        active=bm.select_history.active
        if not isinstance(active,bmesh.types.BMEdge):
            sel=[e for e in bm.edges if e.select]
            if not sel: self.report({'ERROR'}, "Select one boundary edge.")
            return {'CANCELLED'}
            active=sel[0]
        loop=follow_boundary_loop(bm, active)
        if not loop:
            self.report({'ERROR'}, "Not a boundary or loop not found.")
            return {'CANCELLED'}
        for e in bm.edges:
            e.select=False
        for e in loop:
            e.select=True
        bmesh.update_edit_mesh(obj.data)
        self.report({'INFO'}, f"Selected {len(loop)} boundary edges. Press F to fill, or Auto Patch Grid Fill.")
        return {'FINISHED'}

class BoundaryAlignedRemesher:
    def __init__(self, obj):
        self.obj = obj
        mode = obj.mode
        self.edit_mode = mode == 'EDIT'

        # hack to update the mesh data
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.mode_set(mode=mode)

        self.bm = bmesh.new()
        self.bm.from_mesh(obj.data)
        self.bm1 = None

        # If in Edit Mode, operate only on selected faces:
        if self.edit_mode:
            self.bm1 = self.bm.copy()
            remove, remove1 = [], []

            for vert in self.bm.verts:
                if all(not f.select for f in vert.link_faces):
                    remove.append(vert)
            for vert in remove:
                self.bm.verts.remove(vert)

            for vert in self.bm1.verts:
                if all(v.select for v in vert.link_faces):
                    remove1.append(vert)
            for vert in remove1:
                self.bm1.verts.remove(vert)

            remove1 = [f for f in self.bm1.faces if f.select]
            for face in remove1:
                self.bm1.faces.remove(face)

        self.bvh = BVHTree.FromBMesh(self.bm)

        # Boundary guidance data (original)
        self.boundary_data = []
        for edge in self.bm.edges:
            if edge.is_boundary:
                vec = (edge.verts[0].co - edge.verts[1].co).normalized()
                center = (edge.verts[0].co + edge.verts[1].co) / 2
                self.boundary_data.append((center, vec))

        self.boundary_kd_tree = KDTree(len(self.boundary_data))
        for index, (center, vec) in enumerate(self.boundary_data):
            self.boundary_kd_tree.insert(center, index)
        self.boundary_kd_tree.balance()

    def nearest_boundary_vector(self, location):
        location, index, dist = self.boundary_kd_tree.find(location)
        location, vec = self.boundary_data[index]
        return vec

    def enforce_edge_length(self, edge_length=0.05, bias=0.333):
        upper_length = edge_length + edge_length * bias
        lower_length = edge_length - edge_length * bias

        # Subdivide Long edges
        subdivide = []
        for edge in self.bm.edges:
            if edge.calc_length() > upper_length:
                subdivide.append(edge)
        bmesh.ops.subdivide_edges(self.bm, edges=subdivide, cuts=1)
        bmesh.ops.triangulate(self.bm, faces=self.bm.faces)

        if self.edit_mode and self.bm1:
            subdivide = []
            for edge in self.bm1.edges:
                if edge.select and edge.calc_length() > upper_length:
                    subdivide.append(edge)
            bmesh.ops.subdivide_edges(self.bm1, edges=subdivide, cuts=1)

        # Remove verts with less than 5 edges (not on boundaries)
        dissolve_verts = []
        for vert in self.bm.verts:
            if len(vert.link_edges) < 5 and not vert.is_boundary:
                dissolve_verts.append(vert)
        bmesh.ops.dissolve_verts(self.bm, verts=dissolve_verts)
        bmesh.ops.triangulate(self.bm, faces=self.bm.faces)

        # Collapse short edges, avoid boundaries and chaining
        lock_verts = set(vert for vert in self.bm.verts if vert.is_boundary)
        collapse = []
        for edge in self.bm.edges:
            if edge.calc_length() < lower_length and not edge.is_boundary:
                verts = set(edge.verts)
                if verts & lock_verts:
                    continue
                collapse.append(edge)
                lock_verts |= verts
        bmesh.ops.collapse(self.bm, edges=collapse)
        bmesh.ops.beautify_fill(self.bm, faces=self.bm.faces, method="ANGLE")

    def align_verts(self, rule=(-1, -2, -3, -4)):
        # Align verts to nearest boundary by averaging neighbor locations (original logic)
        for vert in self.bm.verts:
            if not vert.is_boundary:
                vec = self.nearest_boundary_vector(vert.co)
                neighbor_locations = [edge.other_vert(vert).co for edge in vert.link_edges]
                best_locations = sorted(
                    neighbor_locations,
                    key=lambda n_loc: abs((n_loc - vert.co).normalized().dot(vec))
                )
                co = vert.co.copy()
                le = len(vert.link_edges)
                for i in rule:
                    co += best_locations[i % le]
                co /= len(rule) + 1
                co -= vert.co
                co -= co.dot(vert.normal) * vert.normal
                vert.co += co

        self.reproject()

    def reproject(self):
        # Recover original shape (original logic)
        for vert in self.bm.verts:
            if vert.is_boundary:
                continue
            location, normal, index, dist = self.bvh.find_nearest(vert.co)
            if location:
                vert.co = location

    def remesh(self, edge_length, iterations, quads):
        # Switch to Object mode if we started in Edit mode (original)
        if self.edit_mode:
            bpy.ops.object.mode_set(mode='OBJECT')

        rule = (-1, -2, 0, 1) if quads else (0, 1, 2, 3)

        for _ in range(iterations):
            self.enforce_edge_length(edge_length=edge_length)
            self.align_verts(rule=rule)
            self.reproject()

        if quads:
            bmesh.ops.join_triangles(
                self.bm, faces=self.bm.faces,
                angle_face_threshold=3.14,
                angle_shape_threshold=3.14
            )

        # Select affected region
        for vert in self.bm.verts:
            vert.select = True
        for face in self.bm.faces:
            face.select = True

        # Merge back into original mesh if we split earlier (original)
        if self.bm1:
            self.bm1.to_mesh(self.obj.data)
            self.bm.from_mesh(self.obj.data)
            bmesh.ops.remove_doubles(
                self.bm, verts=[v for v in self.bm.verts if v.select], dist=0.00001
            )

        self.bm.to_mesh(self.obj.data)

        if self.edit_mode:
            bpy.ops.object.mode_set(mode='EDIT')


# ===================== UI (GROUPED) =====================

class VIEW3D_PT_surface_clusters(bpy.types.Panel):
    bl_label="Surface Clusters"
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category='Ted'
    def draw(self, ctx):
        p=ctx.scene.fumes_cluster
        col=self.layout.column(align=True)
        
        col.prop(p,"min_faces")
        col.prop(p,"angle_start")
        col.prop(p,"angle_grow")
        col.prop(p,"prune_leaves")
        col.operator("mesh.assign_cluster_ids")
        col.operator("mesh.select_faces_by_cluster_id")
        col.operator("mesh.select_all_cluster_ids")
        col.operator("mesh.select_surface_cluster")
        col.operator("object.cluster_remesh")
        
        col.separator()

class VIEW3D_PT_holes(bpy.types.Panel):
    bl_label = "Hole Perimeter Tools"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ted'
    def draw(self, ctx):
        col = self.layout.column(align=True)
        col.operator("mesh.select_hole_perimeter", icon='SELECT_INTERSECT')
        col.operator("mesh.auto_patch_grid_fill", icon='MESH_GRID')




# ===================== MATERIAL TO AREA INDEX =====================

DEBUG_COLORS = [
    (1.0, 0.0, 0.0, 1.0),   # red
    (0.0, 1.0, 0.0, 1.0),   # green
    (0.0, 0.0, 1.0, 1.0),   # blue
    (1.0, 1.0, 0.0, 1.0),   # yellow
    (1.0, 0.0, 1.0, 1.0),   # magenta
    (0.0, 1.0, 1.0, 1.0),   # cyan
    (1.0, 0.5, 0.0, 1.0),   # orange
    (0.5, 0.0, 1.0, 1.0),   # purple
    (0.5, 0.5, 0.5, 1.0),   # gray (extra slot)
]

class AREAINDEX_OT_from_materials(bpy.types.Operator):
    bl_idname = "uv.areaindex_from_materials"
    bl_label = "Assign Areas from Materials"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "For each materal, generate a unique id and assign it as an attribute (AreaIndex) to all related faces"

    def execute(self, context):
        obj = context.object
        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Select a mesh object")
            return {'CANCELLED'}

        mesh = obj.data
        uv_name = "AreaIndex"
        color_name = "AreaColor"

        if uv_name not in mesh.uv_layers:
            mesh.uv_layers.new(name=uv_name)

        if color_name not in mesh.color_attributes:
            mesh.color_attributes.new(name=color_name, type='BYTE_COLOR', domain='CORNER')

        mat_to_index = {}
        for i, slot in enumerate(obj.material_slots):
            if i >= 9:
                break
            mat_to_index[slot.material] = i

        import bmesh
        bm = bmesh.new()
        bm.from_mesh(mesh)

        uv_layer = bm.loops.layers.uv.get(uv_name) or bm.loops.layers.uv.new(uv_name)
        color_layer = bm.loops.layers.color.get(color_name) or bm.loops.layers.color.new(color_name)

        for face in bm.faces:
            mat = obj.material_slots[face.material_index].material if face.material_index < len(obj.material_slots) else None
            if mat not in mat_to_index:
                continue
            idx = mat_to_index[mat]
            u_val = idx * 0.125
            v_val = 0.0
            color = DEBUG_COLORS[idx]
            for loop in face.loops:
                loop[uv_layer].uv = (u_val, v_val)
                loop[color_layer] = color

        bm.to_mesh(mesh)
        bm.free()

        self.report({'INFO'}, f"Assigned {len(mat_to_index)} materials to AreaIndex UVs and AreaColor")
        return {'FINISHED'}


class AREAINDEX_PT_panel(bpy.types.Panel):
    bl_label = "UV Index (Materials)"
    bl_idname = "AREAINDEX_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ted'

    def draw(self, context):
        layout = self.layout
        layout.operator("uv.areaindex_from_materials", text="Assign Areas from Materials")


#------------------------------------------------
# Misc
#------------------------------------------------

import random

class OBJECT_OT_assign_random_colors(bpy.types.Operator):
    bl_idname = "object.assign_random_colors"
    bl_label = "Add material to all objects"
    bl_description = "Assign a random material and color to every mesh object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        for obj in bpy.data.objects:
            if obj.type == 'MESH':
                mat = bpy.data.materials.new(name=f"RandomColor_{obj.name}")
                mat.use_nodes = True
                bsdf = mat.node_tree.nodes.get("Principled BSDF")
                if bsdf:
                    bsdf.inputs['Base Color'].default_value = (
                        random.random(),
                        random.random(),
                        random.random(),
                        1.0
                    )
                if obj.data.materials:
                    obj.data.materials[0] = mat
                else:
                    obj.data.materials.append(mat)
        self.report({'INFO'}, "Random colors assigned to all mesh objects.")
        return {'FINISHED'}



class MESH_OT_fumes_assign_same_normal(bpy.types.Operator):
    bl_idname = "mesh.fumes_assign_same_normal"
    bl_label = "Assign Same Normal (Active Face)"
    bl_description = "Split normals at the selection boundary and assign the active face normal to all selected faces"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and context.mode == 'EDIT_MESH'

    def execute(self, context):
        import bmesh
        from mathutils import Vector

        obj = context.active_object
        me = obj.data

        bm = bmesh.from_edit_mesh(me)
        bm.faces.ensure_lookup_table()

        af = bm.faces.active
        if not af:
            self.report({'ERROR'}, "No active face (make one face active).")
            return {'CANCELLED'}


        af_index = af.index  # store before any mode switch invalidates BMesh refs
        src = af.normal.copy()
        if src.length == 0:
            self.report({'ERROR'}, "Active face normal is zero.")
            return {'CANCELLED'}
        src.normalize()

        sel_faces = [f for f in bm.faces if f.select]
        if not sel_faces:
            self.report({'ERROR'}, "No selected faces.")
            return {'CANCELLED'}
        if af not in sel_faces:
            sel_faces.append(af)

        # Split only at the selection boundary by marking boundary edges sharp.
        # (Sharp edges are an authoring aid; Unreal cares about the exported corner normals.)
        for e in bm.edges:
            lf = e.link_faces
            if not lf:
                continue
            if len(lf) == 1:
                if lf[0].select:
                    e.smooth = False
            elif len(lf) == 2:
                if lf[0].select != lf[1].select:
                    e.smooth = False

        bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)

        # Apply custom split (corner) normals in OBJECT mode.
        prev_mode = obj.mode
        try:
            bpy.ops.object.mode_set(mode='OBJECT')

            # Build a base array of current corner normals (or poly normals as fallback).
            if hasattr(me, "corner_normals") and len(me.corner_normals) == len(me.loops):
                ln = [cn.vector.copy() for cn in me.corner_normals]
            else:
                ln = [Vector((0.0, 0.0, 1.0)) for _ in range(len(me.loops))]
                for p in me.polygons:
                    n = p.normal
                    for i in range(p.loop_start, p.loop_start + p.loop_total):
                        ln[i] = n

            # Overwrite loops for selected polys (+ active face even if not selected)
            # Note: polygon selection is carried from Edit Mode.
            for p in me.polygons:
                if p.select:
                    for i in range(p.loop_start, p.loop_start + p.loop_total):
                        ln[i] = src

            # If active face wasn't selected, ensure its polygon loops also get overwritten.
            # Map active bmesh face index to mesh polygon index (stable in edit mesh).
            if 0 <= af_index < len(me.polygons):
                p = me.polygons[af_index]
                for i in range(p.loop_start, p.loop_start + p.loop_total):
                    ln[i] = src

            me.normals_split_custom_set(ln)

        finally:
            # Return to previous mode
            try:
                bpy.ops.object.mode_set(mode=prev_mode)
            except Exception:
                pass

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Individual static FBX assets / shared textures
# ---------------------------------------------------------------------------

def _fbx_safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)[:100].strip(' .')
    if not name:
        name = 'Object'
    if name.split('.')[0].upper() in {
        'CON', 'PRN', 'AUX', 'NUL',
        *(f'COM{i}' for i in range(1, 10)),
        *(f'LPT{i}' for i in range(1, 10)),
    }:
        name = '_' + name
    return name


def _fbx_object_names(objects):
    names, used = {}, set()
    for obj in sorted(objects, key=lambda ob: (ob.name.casefold(), ob.name)):
        base = _fbx_safe_name(obj.name)
        name, index = base, 2
        while name.casefold() in used:
            name = f'{base}_{index}'
            index += 1
        used.add(name.casefold())
        names[obj] = name + '.fbx'
    return names


def _fbx_asset_groups(context, selected_only):
    """The top-level parent is an asset, even when a child mesh is selected."""
    scene_objects = set(context.scene.objects)
    roots = {}

    def root_of(obj):
        chain = []
        while obj not in roots:
            chain.append(obj)
            if obj.parent not in scene_objects:
                roots[obj] = obj
                break
            obj = obj.parent
        root = roots[obj]
        for child in chain:
            roots[child] = root
        return root

    selected_roots = {root_of(obj) for obj in context.selected_objects} if selected_only else None
    groups = defaultdict(list)
    for obj in context.scene.objects:
        if obj.type == 'MESH':
            root = root_of(obj)
            if selected_roots is None or root in selected_roots:
                groups[root].append(obj)
    for members in groups.values():
        members.sort(key=lambda obj: obj.name)
    return groups


def _fbx_material_images(materials):
    images, visited = set(), set()

    def visit(tree):
        if tree is None or tree in visited:
            return
        visited.add(tree)
        for node in tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                images.add(node.image)
            elif node.type == 'GROUP':
                visit(node.node_tree)

    for material in materials:
        if material and material.use_nodes:
            visit(material.node_tree)
    return sorted(images, key=lambda image: image.name)


def _fbx_write_image(image, directory):
    """Write current pixels, packed bytes, or the original file without saving the source image."""
    if image.source not in {'FILE', 'GENERATED'} or image.is_multiview:
        raise ValueError(f'Texture "{image.name}" uses {image.source}/multiview; bake it to a single image first')
    source = bpy.path.abspath(image.filepath, library=image.library)
    extension = os.path.splitext(source)[1].lower()
    candidate = os.path.join(directory, '_texture' + (extension or '.png'))
    if image.is_dirty or image.source == 'GENERATED':
        if not image.has_data or not all(image.size):
            raise ValueError(f'Texture "{image.name}" has no pixels to export')
        # Image.copy() does not reliably copy unsaved paint buffers.
        copy = bpy.data.images.new('__TedExportPixels', *image.size,
                                   alpha=True, float_buffer=image.is_float)
        try:
            copy.colorspace_settings.name = image.colorspace_settings.name
            copy.alpha_mode = image.alpha_mode
            pixels = array('f', [0.0]) * len(image.pixels)
            image.pixels.foreach_get(pixels)
            copy.pixels.foreach_set(pixels)
            extension = '.exr' if image.is_float else '.png'
            candidate = os.path.join(directory, '_texture' + extension)
            copy.file_format = 'OPEN_EXR' if image.is_float else 'PNG'
            copy.filepath_raw = candidate
            copy.save()
        finally:
            bpy.data.images.remove(copy)
    elif image.packed_file:
        with open(candidate, 'wb') as stream:
            stream.write(image.packed_file.data)
    elif os.path.isfile(source):
        shutil.copyfile(source, candidate)
    else:
        raise ValueError(f'Missing texture "{image.name}": {source or "no file path"}')
    digest = hashlib.sha256()
    with open(candidate, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return candidate, digest.hexdigest()


@contextmanager
def _fbx_shared_textures(materials, directory, progress=None):
    """Temporarily point FBX at portable images; preserve names and shared materials."""
    os.makedirs(directory, exist_ok=True)
    saved, by_content = [], {}
    try:
        images = _fbx_material_images(materials)
        for index, image in enumerate(images):
            if progress:
                progress(0.12 + 0.18 * index / len(images),
                         f'Textures {index + 1}/{len(images)}: {image.name}')
            candidate, digest = _fbx_write_image(image, directory)
            path = by_content.get(digest)
            if path is None:
                extension = os.path.splitext(candidate)[1]
                stem = _fbx_safe_name(os.path.splitext(image.name)[0])
                path = os.path.join(directory, f'{stem}_{digest[:16]}{extension}')
                os.replace(candidate, path)
                by_content[digest] = path
            else:
                os.remove(candidate)
            saved.append((image, image.filepath_raw, image.source))
            image.filepath_raw = path
        yield len(by_content)
    finally:
        for image, filepath, source in reversed(saved):
            image.filepath_raw = filepath
            if image.source != source:
                image.source = source


def _fbx_material_warnings(materials):
    from bpy_extras.node_shader_utils import PrincipledBSDFWrapper
    warnings = []
    for material in materials:
        if not material.use_nodes:
            continue
        wrapper = PrincipledBSDFWrapper(material, is_readonly=True)
        supported = set()
        for channel in ('base_color', 'specular', 'roughness', 'metallic',
                        'normalmap', 'alpha', 'emission_color'):
            texture = getattr(wrapper, channel + '_texture', None)
            if texture and texture.image:
                supported.add(texture.image)
        procedural = any(node.type.startswith('TEX_') and node.type != 'TEX_IMAGE'
                         for node in material.node_tree.nodes)
        if (wrapper.node_principled_bsdf is None or procedural
                or set(_fbx_material_images([material])) - supported):
            warnings.append(material.name)
    return warnings


class _FBXExportProgress:
    """Window-manager feedback plus an actual bar in Blender's bottom status area."""

    def __init__(self, context):
        self.context = context
        self.window = context.window
        self.scene = context.scene
        self.view_layer = context.view_layer
        self.wm = context.window_manager
        self.factor = 0.0
        self.label = 'Preparing export'
        self.last_redraw = 0.0
        self.phase = ''
        self.draw_callback = self.draw
        self.attached = False

    def draw(self, header, context):
        if context.window == self.window:
            row = header.layout.row()
            row.ui_units_x = 32
            row.progress(factor=self.factor, type='BAR',
                         text=f'FBX {self.factor:.0%} | {self.label[:75]}')

    def __enter__(self):
        self.wm.progress_begin(0, 100)
        if not bpy.app.background:
            bpy.types.STATUSBAR_HT_header.prepend(self.draw_callback)
            self.attached = True
        return self

    def update(self, factor, label):
        self.factor = min(1.0, max(self.factor, factor))
        self.label = label
        self.wm.progress_update(self.factor * 100)
        # Synchronous exporters do not return to Blender's event loop between
        # files. Explicit, throttled redraws keep the status bar visible without
        # introducing threads that access bpy or permitting scene edits mid-export.
        phase = label.split(' ', 1)[0]
        now = time.monotonic()
        if phase != self.phase or now - self.last_redraw >= 0.25 or factor >= 0.97:
            self.phase = phase
            self.last_redraw = now
            self.redraw()

    def redraw(self):
        if bpy.app.background or self.window is None:
            return
        try:
            # Never draw the user's window with the temporary export context.
            with self.context.temp_override(window=self.window, scene=self.scene,
                                            view_layer=self.view_layer):
                bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        except RuntimeError:
            # A missing drawable window must not turn a completed file into an error.
            pass

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self.attached:
                bpy.types.STATUSBAR_HT_header.remove(self.draw_callback)
        finally:
            self.wm.progress_end()
            self.redraw()


def _fbx_export_asset(context, depsgraph, export_scene, root, members, filepath,
                      keep_positions, progress):
    # Include the ancestor chain so the asset's hierarchy and root pivot survive.
    nodes = set(members)
    for member in members:
        while member != root:
            member = member.parent
            nodes.add(member)

    def depth(obj):
        count = 0
        while obj != root:
            count += 1
            obj = obj.parent
        return count

    nodes = sorted(nodes, key=lambda obj: (depth(obj), obj.name))
    copies, meshes, matrices = {}, [], {}
    try:
        depsgraph.update()
        offset = (Vector((0, 0, 0)) if keep_positions else
                  root.evaluated_get(depsgraph).matrix_world.translation.copy())
        for index, source in enumerate(nodes):
            progress(0.9 * index / len(nodes), f'part {index + 1}/{len(nodes)}: {source.name}')
            depsgraph.update()
            evaluated = source.evaluated_get(depsgraph)
            matrix = evaluated.matrix_world.copy()
            matrix.translation -= offset
            mesh = None
            if source.type == 'MESH':
                mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True,
                                                       depsgraph=depsgraph)
                meshes.append(mesh)
                # Resolve object overrides onto the private mesh, avoiding a second
                # mesh copy inside the FBX exporter for OBJECT-linked materials.
                for index, slot in enumerate(evaluated.material_slots):
                    if index < len(mesh.materials):
                        mesh.materials[index] = slot.material
            exported = bpy.data.objects.new(_fbx_safe_name(source.name), mesh)
            copies[source] = exported
            export_scene.collection.objects.link(exported)
            if source != root:
                exported.parent = copies[source.parent]
                exported.matrix_parent_inverse = matrices[source.parent].inverted_safe()
            exported.matrix_basis = matrix
            matrices[source] = matrix

        progress(0.9, f'writing FBX ({len(members)} meshes)')
        objects = list(copies.values())
        with context.temp_override(scene=export_scene, view_layer=export_scene.view_layers[0],
                                   active_object=copies[root], object=copies[root],
                                   selected_objects=objects, selected_editable_objects=objects):
            result = bpy.ops.export_scene.fbx(
                filepath=filepath, check_existing=False,
                use_selection=True, object_types={'MESH', 'EMPTY'},
                use_mesh_modifiers=False, bake_anim=False,
                axis_forward='-Z', axis_up='Y', global_scale=1.0,
                apply_unit_scale=True, apply_scale_options='FBX_SCALE_UNITS',
                # Blender's experimental space baking misplaces nested empty
                # transforms. Let FBX carry axis conversion for hierarchies.
                bake_space_transform=len(nodes) == 1, mesh_smooth_type='OFF',
                path_mode='RELATIVE', embed_textures=False,
            )
        if result != {'FINISHED'}:
            raise RuntimeError(f'FBX export failed for "{root.name}"')
    finally:
        progress(0.98, 'releasing temporary asset meshes')
        # Native bulk removal avoids repeated global reference scans per child.
        if copies or meshes:
            bpy.data.batch_remove(ids=[*copies.values(), *meshes])


def _export_individual_fbx(context, directory, selected_only=False,
                           keep_positions=False, overwrite=False, progress=None):
    started = time.perf_counter()

    def report(factor, label):
        print(f'Ted FBX [{time.perf_counter() - started:.2f}s] {label}', flush=True)
        if progress:
            progress(factor, label)

    report(0.0, 'Preparing export')
    groups = _fbx_asset_groups(context, selected_only)
    if not groups:
        raise ValueError('No mesh objects to export')
    objects = [obj for members in groups.values() for obj in members]
    names = _fbx_object_names(groups)
    os.makedirs(directory, exist_ok=True)
    existing = {name.casefold() for name in os.listdir(directory)}
    if not overwrite and any(name.casefold() in existing for name in names.values()):
        raise ValueError('FBX files already exist in this folder; choose another folder or enable Overwrite FBX Files')

    # Evaluate in an isolated scene so excluded collections are included, and
    # selection, visibility, hierarchy and transforms in the user's scene stay intact.
    scene = bpy.data.scenes.new('__TedFBXEvaluation')
    export_scene = None
    source_scene = context.scene
    try:
        # Keep the large evaluation graph separate from the single-asset FBX scene.
        # Linking/deleting an export object in that graph for every file forced
        # Blender to repeatedly rebuild it and scan unrelated scene instances.
        export_scene = bpy.data.scenes.new('__TedFBXExport')
        for temporary in (scene, export_scene):
            temporary.unit_settings.system = source_scene.unit_settings.system
            temporary.unit_settings.scale_length = source_scene.unit_settings.scale_length
            temporary.frame_set(source_scene.frame_current, subframe=source_scene.frame_subframe)
        report(0.02, 'Preparing evaluation scene')
        for obj in source_scene.objects:
            scene.collection.objects.link(obj)
        view_layer = scene.view_layers[0]
        with context.temp_override(scene=scene, view_layer=view_layer):
            report(0.05, 'Evaluating scene and modifiers')
            depsgraph = context.evaluated_depsgraph_get()
            # Gather evaluated materials too (e.g. a modifier assigning a material).
            materials = {slot.material for obj in objects
                         for slot in obj.evaluated_get(depsgraph).material_slots if slot.material}
            warnings = _fbx_material_warnings(materials)
            with tempfile.TemporaryDirectory(prefix='.ted-fbx-', dir=directory) as staging:
                with _fbx_shared_textures(materials, os.path.join(staging, 'Textures'), report) as texture_count:
                    for index, (root, filename) in enumerate(names.items()):
                        def asset_progress(factor, detail):
                            report(0.30 + 0.60 * (index + factor) / len(names),
                                   f'Exporting asset {index + 1}/{len(names)} | {detail} | {root.name}')

                        _fbx_export_asset(context, depsgraph, export_scene, root, groups[root],
                                          os.path.join(staging, filename), keep_positions, asset_progress)
                    report(0.90, 'Restoring source texture paths')
                # Publish only after every FBX and texture has been generated successfully.
                texture_dir = os.path.join(directory, 'Textures')
                os.makedirs(texture_dir, exist_ok=True)
                textures = os.listdir(os.path.join(staging, 'Textures'))
                publish_count = len(textures) + len(names)
                for index, filename in enumerate(textures):
                    report(0.92 + 0.05 * index / publish_count,
                           f'Publishing texture {index + 1}/{len(textures)}: {filename}')
                    os.replace(os.path.join(staging, 'Textures', filename), os.path.join(texture_dir, filename))
                for index, filename in enumerate(names.values()):
                    report(0.92 + 0.05 * (len(textures) + index) / publish_count,
                           f'Publishing FBX {index + 1}/{len(names)}: {filename}')
                    os.replace(os.path.join(staging, filename), os.path.join(directory, filename))
                report(0.97, 'Cleaning staging directory')
    finally:
        report(0.98, 'Cleaning export scene')
        if export_scene is not None:
            bpy.data.scenes.remove(export_scene)
        report(0.99, 'Cleaning evaluation scene (releasing evaluated meshes)')
        bpy.data.scenes.remove(scene)
    report(1.0, f'Complete: {len(groups)} asset FBX files ({len(objects)} meshes) and {texture_count} textures')
    return len(groups), texture_count, warnings


class OBJECT_OT_ted_export_individual_fbx(bpy.types.Operator):
    bl_idname = 'object.ted_export_individual_fbx'
    bl_label = 'Export Asset FBX Files'
    bl_description = 'Export each top-level parent and its mesh descendants as one Unity FBX with shared textures'

    directory: bpy.props.StringProperty(name='Export Folder', subtype='DIR_PATH')
    filter_folder: bpy.props.BoolProperty(default=True, options={'HIDDEN'})
    selected_only: bpy.props.BoolProperty(
        name='Selected Assets Only', default=False,
        description='Select a parent or any child to export its whole top-level asset; otherwise export all scene assets')
    keep_positions: bpy.props.BoolProperty(
        name='Keep Scene Positions', default=False,
        description='Keep world positions; otherwise put each asset root at zero and preserve the offsets of its parts')
    overwrite: bpy.props.BoolProperty(
        name='Overwrite FBX Files', default=False,
        description='Replace matching FBX files in the chosen folder')

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def invoke(self, context, event):
        if not self.directory:
            self.directory = bpy.path.abspath('//')
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        self.layout.prop(self, 'selected_only')
        self.layout.prop(self, 'keep_positions')
        self.layout.prop(self, 'overwrite')
        self.layout.label(text='One FBX per top-level parent; shared textures')

    def execute(self, context):
        if not self.directory:
            self.report({'ERROR'}, 'Choose an export folder')
            return {'CANCELLED'}
        try:
            with _FBXExportProgress(context) as progress:
                count, textures, warnings = _export_individual_fbx(
                    context, os.path.abspath(bpy.path.abspath(self.directory)),
                    self.selected_only, self.keep_positions, self.overwrite,
                    progress=progress.update)
        except Exception as exc:
            self.report({'ERROR'}, f'Individual FBX export: {exc}')
            return {'CANCELLED'}
        if warnings:
            print('Ted FBX: materials needing Unity setup or texture baking:', ', '.join(warnings))
            self.report({'WARNING'}, f'Exported {count} FBX files, {textures} textures; '
                        f'{len(warnings)} materials need baking/setup (see console)')
        else:
            self.report({'INFO'}, f'Exported {count} FBX files and {textures} shared textures')
        return {'FINISHED'}


class VIEW3D_PT_misc(bpy.types.Panel):
    bl_label = "Misc"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Ted'

    def draw(self, ctx):
        col = self.layout.column(align=True)
        col.operator("mesh.select_hole_perimeter", icon='SELECT_INTERSECT')
        col.separator()
        col.operator("mesh.fumes_assign_same_normal", icon='NORMALS_FACE')
        col.operator("object.assign_random_colors", icon='COLOR')
        col.separator()
        col.operator("object.ted_export_individual_fbx", icon='EXPORT')



# ===================== REGISTER =====================
classes = (
    AREAINDEX_OT_from_materials,
    AREAINDEX_PT_panel,
    # surface clusters
    MESH_OT_assign_cluster_ids,
    MESH_OT_select_faces_by_cluster_id,
    MESH_OT_expand_selection,
    MESH_OT_select_all_cluster_ids,
    # Hole
    MESH_OT_select_hole_perimeter, 
    # UI
    VIEW3D_PT_surface_clusters,
    # settings
    FumesClusterSettings,    
    # misc
    OBJECT_OT_assign_random_colors,
    MESH_OT_fumes_assign_same_normal,
    OBJECT_OT_ted_export_individual_fbx,
    VIEW3D_PT_misc,
)

def register():
    for c in classes: bpy.utils.register_class(c)
    bpy.types.Scene.fumes_cluster = bpy.props.PointerProperty(type=FumesClusterSettings)


def unregister():
    for c in reversed(classes): bpy.utils.unregister_class(c)
    del bpy.types.Scene.fumes_cluster

if __name__ == "__main__":
    register()
