targetScope = 'resourceGroup'

@description('Short lowercase prefix used in globally unique resource names.')
@minLength(3)
@maxLength(20)
param namePrefix string

@description('Container image containing the cost enforcement API. Leave empty to use the image repository and tag in the registry created by this template.')
param containerImage string = ''

@description('Repository name used for the remotely built image.')
param imageRepository string = 'cost-enforcement-api'

@description('Tag used for the remotely built image.')
param imageTag string = 'python3-fix'

@description('Deploy the Container App. Set to false for the initial registry bootstrap, then remotely build the image and redeploy with true.')
param deployContainerApp bool = true

@description('Entra tenant that issues APIM managed identity tokens.')
param tenantId string = tenant().tenantId

@description('Audience exposed by the cost API. Use the service principal name registered in Entra, such as the bare application ID.')
param audience string

@description('Client ID of the APIM managed identity allowed to call the API.')
param apimManagedIdentityClientId string

@description('Allow unauthenticated access to admin budget endpoints for prototype testing. Keep false outside controlled testing.')
param adminAuthDisabled bool = false

@secure()
@description('Shared key for admin budget endpoints. Leave empty to keep those endpoints disabled.')
param adminApiKey string = ''

@description('Monthly caller budget in decimal USD.')
param monthlyLimitUsd string = '100.00'

@description('Address space for the cost enforcement virtual network.')
param vnetAddressPrefix string = '10.20.0.0/16'

@description('Dedicated subnet for the workload-profile Container Apps environment.')
param containerAppsSubnetPrefix string = '10.20.0.0/27'

@description('Dedicated subnet for private endpoints.')
param privateEndpointSubnetPrefix string = '10.20.1.0/24'

param location string = resourceGroup().location

var uniqueSuffix = uniqueString(subscription().subscriptionId, resourceGroup().id)
var cosmosName = take('${namePrefix}-${uniqueSuffix}', 44)
var registryName = toLower(take('${replace(namePrefix, '-', '')}${uniqueSuffix}', 50))
var vnetName = '${namePrefix}-cost-vnet'
var containerAppsSubnetName = 'container-apps-infrastructure'
var privateEndpointSubnetName = 'private-endpoints'
var environmentName = '${namePrefix}-vnet-env'
var appName = '${namePrefix}-vnet-api'
var pullIdentityName = '${namePrefix}-pull'
var cosmosPrivateEndpointName = '${namePrefix}-cosmos-pe'
var cosmosPrivateDnsZoneName = 'privatelink.documents.azure.com'
var acrPullRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var effectiveContainerImage = empty(containerImage) ? '${registry.properties.loginServer}/${imageRepository}:${imageTag}' : containerImage

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: containerAppsSubnetName
        properties: {
          addressPrefix: containerAppsSubnetPrefix
          delegations: [
            {
              name: 'Microsoft.App.environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
    ]
  }
}

resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: containerAppsSubnetName
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: privateEndpointSubnetName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: pullIdentityName
  location: location
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, pullIdentity.id, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    principalId: pullIdentity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: cosmosName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    ipRules: []
    minimalTlsVersion: 'Tls12'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: 'cost-enforcement'
  properties: {
    resource: {
      id: 'cost-enforcement'
    }
  }
}

resource ledger 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: 'ledger'
  properties: {
    resource: {
      id: 'ledger'
      partitionKey: {
        paths: [
          '/partitionKey'
        ]
        kind: 'Hash'
        version: 2
      }
      conflictResolutionPolicy: {
        mode: 'LastWriterWins'
        conflictResolutionPath: '/_ts'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/usage/*'
          }
          {
            path: '/"_etag"/?'
          }
        ]
      }
    }
  }
}

resource pricing 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  parent: database
  name: 'pricing'
  properties: {
    resource: {
      id: 'pricing'
      partitionKey: {
        paths: [
          '/deployment'
        ]
        kind: 'Hash'
        version: 2
      }
      conflictResolutionPolicy: {
        mode: 'LastWriterWins'
        conflictResolutionPath: '/_ts'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/"_etag"/?'
          }
        ]
      }
    }
  }
}

resource cosmosPrivateDnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: cosmosPrivateDnsZoneName
  location: 'global'
}

resource cosmosPrivateDnsVnetLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: cosmosPrivateDnsZone
  name: '${namePrefix}-cost-vnet-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource cosmosPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: cosmosPrivateEndpointName
  location: location
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: '${cosmosPrivateEndpointName}-connection'
        properties: {
          privateLinkServiceId: cosmos.id
          groupIds: [
            'Sql'
          ]
          requestMessage: 'Private access for the cost enforcement API.'
        }
      }
    ]
  }
}

resource cosmosPrivateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: cosmosPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'cosmos-sql'
        properties: {
          privateDnsZoneId: cosmosPrivateDnsZone.id
        }
      }
    ]
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: containerAppsSubnet.id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployContainerApp) {
  name: appName
  location: location
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${pullIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: empty(adminApiKey) ? [] : [
        {
          name: 'admin-api-key'
          value: adminApiKey
        }
      ]
      registries: [
        {
          server: registry.properties.loginServer
          identity: pullIdentity.id
        }
      ]
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          name: 'api'
          image: effectiveContainerImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat([
            { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
            { name: 'COSMOS_DATABASE', value: database.name }
            { name: 'COSMOS_LEDGER_CONTAINER', value: ledger.name }
            { name: 'COSMOS_PRICING_CONTAINER', value: pricing.name }
            { name: 'MONTHLY_LIMIT_USD', value: monthlyLimitUsd }
            { name: 'TENANT_ID', value: tenantId }
            { name: 'AUDIENCE', value: audience }
            { name: 'ALLOWED_CLIENT_ID', value: apimManagedIdentityClientId }
            { name: 'ADMIN_AUTH_DISABLED', value: string(adminAuthDisabled) }
          ], empty(adminApiKey) ? [] : [
            { name: 'ADMIN_API_KEY', secretRef: 'admin-api-key' }
          ])
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 10
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
  dependsOn: [
    acrPull
    cosmosPrivateDnsZoneGroup
    cosmosPrivateDnsVnetLink
  ]
}

resource cosmosDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (deployContainerApp) {
  parent: cosmos
  name: guid(cosmos.id, app.id, 'cosmos-data-contributor')
  properties: {
    principalId: app.?identity.principalId ?? ''
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmos.id
  }
}

output apiUrl string = deployContainerApp ? 'https://${app.?properties.configuration.ingress.fqdn ?? ''}' : ''
output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output defaultContainerImage string = '${registry.properties.loginServer}/${imageRepository}:${imageTag}'
output cosmosAccountName string = cosmos.name
output containerAppPrincipalId string = app.?identity.principalId ?? ''
