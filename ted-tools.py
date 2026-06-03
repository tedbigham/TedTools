bl_info = {
    "name": "Ted Tools",
    "author": "Ted Bigham",
    "version": (2,1,0),
    "blender": (4, 5, 0),
    "location": "3D View > N‑Panel > Ted",
    "description": "Assortment of technical tools.",
    "category": "Mesh",
}

__version__ = bl_info["version"]
__version_str__ = ".".join(str(x) for x in __version__)

import bpy, bmesh, math, colorsys
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
