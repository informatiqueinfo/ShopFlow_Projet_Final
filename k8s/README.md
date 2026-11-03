# Démonstration Kubernetes 

Docker Desktop peut héberger un cluster Kubernetes interne visible par `kubectl` mais non listé par `kind get clusters`. Pour charger une image locale avec `kind load docker-image`, le chemin le plus simple est un cluster kind **créé explicitement**. Ne pas effacer le cluster Docker Desktop existant.

```bash
# Docker Desktop démarré, kind et kubectl installés
kind create cluster --name shopflow-lab
docker build -t shopflow-final:1.0 .
kind load docker-image shopflow-final:1.0 --name shopflow-lab
kubectl apply -f k8s/resources.yaml
kubectl -n shopflow rollout status deployment/shopflow-gateway --timeout=120s
kubectl -n shopflow get pods -o wide
```

Dans un second terminal :

```bash
kubectl -n shopflow port-forward svc/shopflow-gateway 8000:8000
```

Dans un troisième :

```bash
curl -i http://localhost:8000/catalogue
kubectl -n shopflow get pods
# Reprendre le nom exact d'un pod gateway de la ligne précédente
kubectl -n shopflow delete pod NOM_DU_POD
kubectl -n shopflow get pods --watch
```

Kubernetes doit créer un pod portant un **nouveau nom** pour maintenir `replicas: 3`.

Pour un rolling update :

```bash
docker build -t shopflow-final:1.1 .
kind load docker-image shopflow-final:1.1 --name shopflow-lab
kubectl -n shopflow set image deployment/shopflow-gateway gateway=shopflow-final:1.1
kubectl -n shopflow rollout status deployment/shopflow-gateway --timeout=120s
kubectl -n shopflow get deployments,pdb
```
